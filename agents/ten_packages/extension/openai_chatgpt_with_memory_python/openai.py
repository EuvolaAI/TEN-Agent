#
#
# Agora Real Time Engagement
# Copyright (c) 2024 Agora IO. All rights reserved.
#
#
from collections import defaultdict
from dataclasses import dataclass
import random
import requests
from openai import AsyncOpenAI, AsyncAzureOpenAI
from openai.types.chat.chat_completion import ChatCompletion

from ten.async_ten_env import AsyncTenEnv
from ten_ai_base.config import BaseConfig


@dataclass
class OpenAIChatGPTWithMemoryConfig(BaseConfig):
    api_key: str = ""
    base_url: str = "https://api.openai.com/v1"
    model: str = (
        "gpt-4o"  # Adjust this to match the equivalent of `openai.GPT4o` in the Python library
    )
    prompt: str = (
        "你是一个具有长期记忆能力的智能助手，能够记住并利用过去的对话内容。当用户提到过去讨论过的内容时，你应该能够回忆起相关信息并保持对话的连贯性。以友好、自然的方式回应，避免机械式回复。"
    )
    frequency_penalty: float = 0.9
    presence_penalty: float = 0.9
    top_p: float = 1.0
    temperature: float = 0.1
    max_tokens: int = 512
    seed: int = random.randint(0, 10000)
    proxy_url: str = ""
    greeting: str = "你好！我是带有长期记忆的TEN Agent。我能记住我们之间的对话，有什么我能帮助你的吗？"
    max_memory_length: int = 15
    vendor: str = "openai"
    azure_endpoint: str = ""
    azure_api_version: str = ""
    
    # 向量数据库配置
    qdrant_host: str = "qdrant"
    qdrant_port: int = 6333

class ThinkParser:
    def __init__(self):
        self.state = 'NORMAL'  # States: 'NORMAL', 'THINK'
        self.think_content = ""
        self.content = ""
    
    def process(self, new_chars):
        if new_chars == "<think>":
            self.state = 'THINK'
            return True
        elif new_chars == "</think>":
            self.state = 'NORMAL'
            return True
        else:
            if self.state == "THINK":
                self.think_content += new_chars
        return False
        

class OpenAIChatGPT:
    client = None

    def __init__(self, ten_env: AsyncTenEnv, config: OpenAIChatGPTWithMemoryConfig):
        self.config = config
        self.ten_env = ten_env
        ten_env.log_info(f"OpenAIChatGPT initialized with config: {config.api_key}")
        if self.config.vendor == "azure":
            self.client = AsyncAzureOpenAI(
                api_key=config.api_key,
                api_version=self.config.azure_api_version,
                azure_endpoint=config.azure_endpoint,
            )
            ten_env.log_info(
                f"Using Azure OpenAI with endpoint: {config.azure_endpoint}, api_version: {config.azure_api_version}"
            )
        else:
            self.client = AsyncOpenAI(api_key=config.api_key, base_url=config.base_url, default_headers={
                "api-key": config.api_key,
            })
        self.session = requests.Session()
        if config.proxy_url:
            proxies = {
                "http": config.proxy_url,
                "https": config.proxy_url,
            }
            ten_env.log_info(f"Setting proxies: {proxies}")
            self.session.proxies.update(proxies)
        self.client.session = self.session

    async def get_chat_completions(self, messages, tools=None) -> ChatCompletion:
        req = {
            "model": self.config.model,
            "messages": [
                {
                    "role": "system",
                    "content": self.config.prompt,
                },
                *messages,
            ],
            "tools": tools,
            "temperature": self.config.temperature,
            "top_p": self.config.top_p,
            "presence_penalty": self.config.presence_penalty,
            "frequency_penalty": self.config.frequency_penalty,
            "max_tokens": self.config.max_tokens,
            "seed": self.config.seed,
        }

        try:
            response = await self.client.chat.completions.create(**req)
        except Exception as e:
            raise RuntimeError(f"CreateChatCompletion failed, err: {e}") from e

        return response

    async def get_chat_completions_stream(self, messages, tools=None, listener=None):
        req = {
            "model": self.config.model,
            "messages": [
                {
                    "role": "system",
                    "content": self.config.prompt,
                },
                *messages,
            ],
            "tools": tools,
            "temperature": self.config.temperature,
            "top_p": self.config.top_p,
            "presence_penalty": self.config.presence_penalty,
            "frequency_penalty": self.config.frequency_penalty,
            "max_tokens": self.config.max_tokens,
            "seed": self.config.seed,
            "stream": True,
        }

        try:
            response = await self.client.chat.completions.create(**req)
        except Exception as e:
            raise RuntimeError(f"CreateChatCompletionStream failed, err: {e}") from e

        full_content = ""
        # Check for tool calls
        tool_calls_dict = defaultdict(
            lambda: {
                "id": None,
                "function": {"arguments": "", "name": None},
                "type": None,
            }
        )

        # Example usage
        parser = ThinkParser()

        async for chat_completion in response:
            # self.ten_env.log_info(f"Chat completion: {chat_completion}")
            if len(chat_completion.choices) == 0:
                continue
            choice = chat_completion.choices[0]
            delta = choice.delta

            content = delta.content if delta and delta.content else ""

            # Emit content update event (fire-and-forget)
            if listener and content:
                prev_state = parser.state
                is_special_char = parser.process(content)

                if not is_special_char:
                    # self.ten_env.log_info(f"state: {parser.state}, content: {content}, think: {parser.think_content}")
                    if parser.state == "THINK":
                        listener.emit("reasoning_update", parser.think_content)
                    elif parser.state == "NORMAL":
                        listener.emit("content_update", content)

                if prev_state == "THINK" and parser.state == "NORMAL":
                    listener.emit("reasoning_update_finish", parser.think_content)
                    parser.think_content = ""

            full_content += content

            if delta.tool_calls:
                for tool_call in delta.tool_calls:
                    if tool_call.id is not None:
                        tool_calls_dict[tool_call.index]["id"] = tool_call.id

                    # If the function name is not None, set it
                    if tool_call.function.name is not None:
                        tool_calls_dict[tool_call.index]["function"][
                            "name"
                        ] = tool_call.function.name

                    # Append the arguments
                    tool_calls_dict[tool_call.index]["function"][
                        "arguments"
                    ] += tool_call.function.arguments

                    # If the type is not None, set it
                    if tool_call.type is not None:
                        tool_calls_dict[tool_call.index]["type"] = tool_call.type

        # Convert the dictionary to a list
        tool_calls_list = list(tool_calls_dict.values())

        # Emit tool calls event (fire-and-forget)
        if listener and tool_calls_list:
            for tool_call in tool_calls_list:
                listener.emit("tool_call", tool_call)

        # Emit content finished event after the loop completes
        if listener:
            listener.emit("content_finished", full_content) 