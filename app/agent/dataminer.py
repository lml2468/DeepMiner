from pathlib import Path

from pydantic import Field

from app.common.config import config
from app.core.toolcall import ToolCallAgent
from app.prompt.dataminer import NEXT_STEP_PROMPT, SYSTEM_PROMPT
from app.tool import Terminate, ToolCollection, CreateChatCompletion
from app.tool.file_saver import FileSaver
from app.tool.python_execute import PythonExecute


class DataMiner(ToolCallAgent):
    """
    A specialized execution agent focused on data processing, analysis, and visualization.

    This agent specializes in:
    - Python-based data analysis, transformation, and visualization
    - Structured data handling and insight generation
    - Local file storage and data persistence

    In the agent ecosystem, DeepMiner focuses specifically on data-centric operations,
    complementing the SWE agent (system-level programming and development) and
    Web agent (online information retrieval and browsing). Together with a Planning agent,
    they collectively form a complete general-purpose agent ecosystem following the MECE principle.
    """

    name: str = "DataMiner"
    description: str = (
        "A specialized execution agent focused on data processing, analysis, and visualization, "
        "forming part of a MECE-based general-purpose agent ecosystem alongside SWE and Web agents"
    )

    system_prompt: str = SYSTEM_PROMPT
    next_step_prompt: str = NEXT_STEP_PROMPT
    working_dir: Path = config.workspace_root

    max_observe: int = 2000
    max_steps: int = 20

    # Add general-purpose tools to the tool collection
    available_tools: ToolCollection = Field(
        default_factory=lambda: ToolCollection(
            PythonExecute(), CreateChatCompletion(), FileSaver(), Terminate()
        )
    )

    async def think(self) -> bool:
        self.next_step_prompt = self.next_step_prompt.format(
            working_dir=self.working_dir
        )

        return await super().think()
