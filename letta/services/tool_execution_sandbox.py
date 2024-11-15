import ast
import os
import subprocess
import tempfile
import venv
from typing import Any, Optional

from letta.log import get_logger
from letta.schemas.sandbox_config import SandboxConfig, SandboxType
from letta.services.sandbox_config_manager import SandboxConfigManager
from letta.services.tool_manager import ToolManager
from letta.services.user_manager import UserManager
from letta.settings import tool_settings

logger = get_logger(__name__)


class ToolExecutionSandbox:
    DIR = "/home/user/"
    METADATA_CONFIG_STATE_KEY = "config_state"

    def __init__(self, tool_name: str, args: dict, user_id: str):
        from letta.server.server import db_context

        self.session_maker = db_context
        self.tool_name = tool_name
        self.args = args

        # Get the user
        # This user corresponds to the agent_state's user_id field
        # agent_state is the state of the agent that invoked this run
        self.user = UserManager().get_user_by_id(user_id=user_id)

        # Get the tool
        # TODO: So in theory, it's possible this retrieves a tool not provisioned to the agent
        # TODO: That would probably imply that agent_state is incorrectly configured
        self.tool = ToolManager().get_tool_by_name(tool_name=tool_name, actor=self.user)
        self.sandbox_config_manager = SandboxConfigManager()

    def run(self) -> Optional[Any]:
        if not self.tool:
            return f"Agent attempted to invoke tool {self.tool_name} that does not exist for organization {self.user.organization_id}"
        if tool_settings.e2b_api_key:
            logger.info("Using e2b for tool execution...")
            code = self.generate_execution_script(wrap_print=False)
            return self.run_e2b_sandbox(code=code)
        else:
            logger.info("Using local sandbox for tool execution...")
            code = self.generate_execution_script(wrap_print=True)
            return self.run_local_dir_sandbox(code=code)

    def run_local_dir_sandbox(self, code: str) -> Optional[Any]:
        sbx_config = self.sandbox_config_manager.get_or_create_default_sandbox_config(sandbox_type=SandboxType.LOCAL, actor=self.user)
        local_configs = sbx_config.get_local_config()

        # Get environment variables for the sandbox
        # TODO: We set limit to 100 here, but maybe we want it uncapped? Realistically this should be fine.
        env_vars = self.sandbox_config_manager.get_sandbox_env_vars_as_dict(sandbox_config_id=sbx_config.id, actor=self.user, limit=100)

        env = os.environ.copy()
        venv_path = os.path.join(local_configs.sandbox_dir, local_configs.venv_name)
        env["VIRTUAL_ENV"] = venv_path
        env["PATH"] = os.path.join(venv_path, "bin") + ":" + env["PATH"]
        env.update(env_vars)

        # Safety checks
        # Check that sandbox_dir exists
        if not os.path.isdir(local_configs.sandbox_dir):
            raise FileNotFoundError(f"Sandbox directory does not exist: {local_configs.sandbox_dir}")
        # Verify that the venv path exists and is a directory
        if not os.path.isdir(venv_path):
            logger.warning(f"Virtual environment directory does not exist at: {venv_path}, creating one now...")
            venv.create(venv_path, with_pip=True)

        # Ensure the python interpreter exists in the virtual environment
        python_executable = os.path.join(venv_path, "bin", "python3")
        if not os.path.isfile(python_executable):
            raise FileNotFoundError(f"Python executable not found in virtual environment: {python_executable}")

        # Write the code to a temp file in the sandbox_dir
        with tempfile.NamedTemporaryFile(mode="w", dir=local_configs.sandbox_dir, suffix=".py", delete=True) as temp_file:
            temp_file.write(code)
            temp_file.flush()  # Ensure all data is written to disk

            # Execute the code in a restricted subprocess
            try:
                result = subprocess.run(
                    [os.path.join(venv_path, "bin", "python3"), temp_file.name],
                    env=env,
                    cwd=local_configs.sandbox_dir,  # Restrict execution to sandbox_dir
                    timeout=60,
                    capture_output=True,
                    text=True,
                )
                if result.stderr:
                    raise RuntimeError(f"Sandbox execution error: {result.stderr}")
                return self.ast_parse_best_effort(result.stdout)
            except subprocess.TimeoutExpired:
                raise TimeoutError(f"Executing tool {self.tool_name} has timed out.")
            except subprocess.CalledProcessError as e:
                raise RuntimeError(f"Executing tool {self.tool_name} has process error: {e}")
            except Exception as e:
                raise RuntimeError(f"Executing tool {self.tool_name} has an unexpected error: {e}")

    def run_e2b_sandbox(self, code: str) -> Optional[Any]:
        from e2b_code_interpreter import Sandbox

        sbx_config = self.sandbox_config_manager.get_or_create_default_sandbox_config(sandbox_type=SandboxType.E2B, actor=self.user)
        sbx = self.get_running_e2b_sandbox_with_same_state(sbx_config)
        if not sbx:
            sbx = Sandbox(metadata={self.METADATA_CONFIG_STATE_KEY: self.user.organization_id}, **sbx_config.config)

        # Get environment variables for the sandbox
        # TODO: We set limit to 100 here, but maybe we want it uncapped? Realistically this should be fine.
        env_vars = self.sandbox_config_manager.get_sandbox_env_vars_as_dict(sandbox_config_id=sbx_config.id, actor=self.user, limit=100)
        execution = sbx.run_code(code, envs=env_vars)
        if execution.error is not None:
            raise Exception(f"Executing tool {self.tool_name} failed with {execution.error}")
        elif len(execution.results) == 0:
            function_response = None
        else:
            function_response = self.ast_parse_best_effort(execution.results[0].text)

        # Note, we don't kill the sandbox
        return function_response

    def get_running_e2b_sandbox_with_same_state(self, sandbox_config: SandboxConfig) -> Optional["Sandbox"]:
        from e2b_code_interpreter import Sandbox

        # List running sandboxes and access metadata.
        running_sandboxes = Sandbox.list()

        # Hash the config to check the state
        state_hash = hash(sandbox_config)
        for sandbox in running_sandboxes:
            if self.METADATA_CONFIG_STATE_KEY in sandbox.metadata and sandbox.metadata[self.METADATA_CONFIG_STATE_KEY] == state_hash:
                return Sandbox.connect(sandbox.sandbox_id)

        return None

    def ast_parse_best_effort(self, text: str) -> Any:
        try:
            result = ast.literal_eval(text)
        except SyntaxError:
            result = text
        except ValueError:
            result = text

        return result

    def generate_execution_script(self, wrap_print: bool = False) -> str:
        code = ""

        for param in self.args:
            code += self.initialize_param(param, self.args[param])

        code += "\n" + self.tool.source_code + "\n"

        code += self.invoke_function_call(wrap_print=wrap_print)

        return code

    def initialize_param(self, name: str, raw_value: str) -> str:
        params = self.tool.json_schema["parameters"]["properties"]
        spec = params.get(name)
        if spec is None:
            # ignore extra params (like 'self') for now
            return ""

        param_type = spec.get("type")
        if param_type is None and spec.get("parameters"):
            param_type = spec["parameters"].get("type")

        if param_type == "string":
            value = '"' + raw_value + '"'
        elif param_type == "integer" or param_type == "boolean":
            value = raw_value
        else:
            raise TypeError(f"unsupported type: {param_type}")

        return name + " = " + str(value) + "\n"

    def invoke_function_call(self, wrap_print: bool = False) -> str:
        kwargs = []
        for name in self.args:
            if name in self.tool.json_schema["parameters"]["properties"]:
                kwargs.append(name)

        params = ", ".join([f"{arg}={arg}" for arg in kwargs])
        func_call_str = self.tool.name + "(" + params + ")"
        if wrap_print:
            func_call_str = f"print({func_call_str})"
        return func_call_str
