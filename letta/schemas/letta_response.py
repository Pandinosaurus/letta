from typing import List, Union

from pydantic import BaseModel, Field

from letta.schemas.enums import MessageStreamStatus
from letta.schemas.letta_message import LettaMessage
from letta.schemas.message import Message
from letta.schemas.usage import LettaUsageStatistics
from letta.utils import json_dumps

# TODO: consider moving into own file


class LettaResponse(BaseModel):
    """
    Response object from an agent interaction, consisting of the new messages generated by the agent and usage statistics.
    The type of the returned messages can be either `Message` or `LettaMessage`, depending on what was specified in the request.

    Attributes:
        messages (List[Union[Message, LettaMessage]]): The messages returned by the agent.
        usage (LettaUsageStatistics): The usage statistics
    """

    messages: Union[List[Message], List[LettaMessage]] = Field(..., description="The messages returned by the agent.")
    usage: LettaUsageStatistics = Field(..., description="The usage statistics of the agent.")

    def __str__(self):
        return json_dumps(
            {
                "messages": [message.model_dump() for message in self.messages],
                # Assume `Message` and `LettaMessage` have a `dict()` method
                "usage": self.usage.model_dump(),  # Assume `LettaUsageStatistics` has a `dict()` method
            },
            indent=4,
        )


# The streaming response is either [DONE], [DONE_STEP], [DONE], an error, or a LettaMessage
LettaStreamingResponse = Union[LettaMessage, MessageStreamStatus, LettaUsageStatistics]
