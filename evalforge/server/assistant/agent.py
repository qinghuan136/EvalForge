from pydantic_ai import Agent, DeferredToolRequests, Tool
from pydantic_ai.models import Model
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from .context import AssistantDeps
from .prompts import INSTRUCTIONS
from .tools import ASSISTANT_TOOLS, WRITE_TOOL_NAMES


def build_model(name: str, api_key: str, base_url: str | None) -> Model:
    return OpenAIChatModel(name, provider=OpenAIProvider(api_key=api_key, base_url=base_url))


def build_agent(model: Model) -> Agent[AssistantDeps, str | DeferredToolRequests]:
    return Agent(model, name="EvalForgeAssistant", deps_type=AssistantDeps, instructions=INSTRUCTIONS,
                 output_type=[str, DeferredToolRequests],
                 tools=[Tool(tool, requires_approval=tool.__name__ in WRITE_TOOL_NAMES,
                             sequential=tool.__name__ in WRITE_TOOL_NAMES) for tool in ASSISTANT_TOOLS])
