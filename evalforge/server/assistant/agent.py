from pydantic_ai import Agent
from pydantic_ai.models import Model
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from .context import AssistantDeps
from .prompts import EXPERIMENT_INSTRUCTIONS, INSTRUCTIONS, NO_EXPERIMENT_INSTRUCTIONS
from .tools import get_experiment_progress


def build_model(name: str, api_key: str, base_url: str | None) -> Model:
    return OpenAIChatModel(name, provider=OpenAIProvider(api_key=api_key, base_url=base_url))


def build_agent(model: Model, *, has_experiment_context: bool) -> Agent[AssistantDeps, str]:
    instructions = INSTRUCTIONS + (EXPERIMENT_INSTRUCTIONS if has_experiment_context else NO_EXPERIMENT_INSTRUCTIONS)
    return Agent(model, name="EvalForgeAssistant", deps_type=AssistantDeps, instructions=instructions,
                 output_type=str, tools=[get_experiment_progress] if has_experiment_context else [])
