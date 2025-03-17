from typing import Any, List, Optional

from pydantic import Field

from app.core.toolcall import ToolCallAgent
from app.prompt.web import NEXT_STEP_PROMPT, SYSTEM_PROMPT
from app.tool import Terminate, ToolCollection
from app.tool.browser_use_tool import BrowserUseTool
from app.tool.file_saver import FileSaver
from app.tool.web_search import WebSearch


class WebAgent(ToolCallAgent):
    """
    An agent that can browse the web, search for information, and interact with web pages.

    This agent uses browser automation tools to navigate websites, extract information,
    and save content to files. It can perform web searches and follow links to gather
    comprehensive information on a topic.
    """

    name: str = "WebAgent"
    description: str = "An agent that can browse the web, search for information, and interact with web pages."

    system_prompt: str = SYSTEM_PROMPT
    next_step_prompt: str = NEXT_STEP_PROMPT

    available_tools: ToolCollection = Field(
        default_factory=lambda: ToolCollection(
            BrowserUseTool(), WebSearch(), FileSaver(), Terminate()
        )
    )
    special_tool_names: List[str] = Field(default_factory=lambda: [BrowserUseTool().name, Terminate().name])

    max_steps: int = 30
    current_url: Optional[str] = None
    search_history: List[str] = Field(default_factory=list)
    saved_files: List[str] = Field(default_factory=list)

    async def think(self) -> bool:
        """Process current state and decide next actions using web tools"""
        # Add current URL to the next step prompt if available
        if self.current_url:
            context_prompt = f"Current URL: {self.current_url}\n{self.next_step_prompt}"
        else:
            context_prompt = self.next_step_prompt

        self.next_step_prompt = context_prompt

        # Use the parent class's think method
        return await super().think()

    async def act(self) -> str:
        """Execute tool calls and track web browsing state"""
        result = await super().act()

        # Update agent state based on the executed tools
        if self.tool_calls:
            for tool_call in self.tool_calls:
                name = tool_call.function.name
                args = tool_call.function.arguments

                # Track browser navigation
                if name == "browser_use" and args:
                    import json
                    try:
                        browser_args = json.loads(args)
                        if browser_args.get("action") == "navigate" and browser_args.get("url"):
                            self.current_url = browser_args.get("url")
                    except Exception:
                        pass

                # Track web searches
                elif name == "web_search" and args:
                    import json
                    try:
                        search_args = json.loads(args)
                        if search_args.get("query"):
                            self.search_history.append(search_args.get("query"))
                    except Exception:
                        pass

                # Track saved files
                elif name == "file_saver" and args:
                    import json
                    try:
                        file_args = json.loads(args)
                        if file_args.get("file_path"):
                            self.saved_files.append(file_args.get("file_path"))
                    except Exception:
                        pass

        return result

    async def _handle_special_tool(self, name: str, result: Any, **kwargs):
        """Handle special tool execution and state changes

        Ensures proper cleanup of browser resources when terminating the agent.
        """
        if not self._is_special_tool(name):
            return
        else:
            # Clean up browser resources before terminating
            browser_tool = self.available_tools.get_tool(BrowserUseTool().name)
            if browser_tool:
                await browser_tool.cleanup()

            # Call parent's special tool handler
            await super()._handle_special_tool(name, result, **kwargs)
