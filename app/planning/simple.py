import json
import uuid
from typing import Dict, List, Optional, Union

from pydantic import Field

from app.common.llm import LLM
from app.common.logger import logger
from app.core.base import BaseAgent
from app.core.schema import AgentState, Message, ToolChoice
from app.planning.base import BasePlanner, PlanStepStatus
from app.tool import PlanningTool


class SimplePlanner(BasePlanner):
    """A simple planner that manages planning and execution of tasks using agents."""

    llm: LLM = Field(default_factory=lambda: LLM())
    planning_tool: PlanningTool = Field(default_factory=PlanningTool)
    executor_keys: List[str] = Field(default_factory=list)
    active_plan_id: str = Field(default_factory=lambda: f"plan_{uuid.uuid4()}")
    current_step_index: Optional[int] = None

    def __init__(
        self, agents: Union[BaseAgent, List[BaseAgent], Dict[str, BaseAgent]], **data
    ):
        # Set executor keys before super().__init__
        if "executors" in data:
            data["executor_keys"] = data.pop("executors")

        # Set plan ID if provided
        if "plan_id" in data:
            data["active_plan_id"] = data.pop("plan_id")

        # Initialize the planning tool if not provided
        if "planning_tool" not in data:
            planning_tool = PlanningTool()
            data["planning_tool"] = planning_tool

        # Call parent's init with the processed data
        super().__init__(agents, **data)

        # Set executor_keys to all agent keys if not specified
        if not self.executor_keys:
            self.executor_keys = list(self.agents.keys())

    async def _get_step_executor_with_llm(self, step_info: dict) -> str:
        """
        Use LLM to determine the most appropriate executor for a given step.

        Args:
            step_info: Dictionary containing step information, including 'text'

        Returns:
            The key of the recommended executor agent
        """
        if not step_info or "text" not in step_info:
            return None

        step_text = step_info["text"]

        # Create a system message that explains the task
        system_message = Message.system_message(
            "You are an agent dispatcher. Your task is to analyze a step description "
            "and determine which specialized agent would be best suited to execute it. "
            "Choose the most appropriate agent based on the step's requirements and each agent's capabilities."
        )

        # Create a prompt that describes available agents and their capabilities
        available_agents_desc = "\n".join([
            f"- {key}: {agent.description}" for key, agent in self.agents.items()
        ])

        user_message = Message.user_message(
            f"Based on the following step description, which agent should execute it?\n\n"
            f"Step: {step_text}\n\n"
            f"Available agents:\n{available_agents_desc}\n\n"
            f"Respond with just the agent key (e.g., 'WebAgent', 'DataMiner', 'SWEAgent') that should handle this step."
        )

        # Call LLM to get recommendation
        # Extract agent key from response (assuming response is just the agent key)
        recommended_agent = await self.llm.ask(
            messages=[user_message],
            system_msgs=[system_message]
        )

        # Validate that the recommended agent exists
        if recommended_agent in self.agents:
            logger.info(f"Automatically selecting agent '{recommended_agent}' for task: {step_text[:50]}...")
            return recommended_agent
        else:
            # If LLM recommends an invalid agent, try to find a close match
            for key in self.agents.keys():
                if key.lower() in recommended_agent.lower() or recommended_agent.lower() in key.lower():
                    logger.info(f"Using closest match '{key}' instead of LLM recommendation '{recommended_agent}'")
                    return key

            # Fall back to default behavior if no match found
            logger.warning(f"Recommended unknown agent '{recommended_agent}', falling back to default selection")
            return None

    async def get_executor(self, step_info: Optional[dict] = None) -> BaseAgent:
        """
        Get an appropriate executor agent for the current step using LLM for intelligent selection.

        Args:
            step_info: Dictionary containing step information

        Returns:
            The selected executor agent
        """
        # If no step info provided, use default selection logic
        if not step_info:
            # Use first available executor or fall back to primary agent
            for key in self.executor_keys:
                if key in self.agents:
                    return self.agents[key]
            return self.primary_agent

        # First check if there's an explicit type tag in the step
        step_type = step_info.get("type")
        if step_type and step_type in self.agents:
            logger.info(f"Using explicitly tagged agent '{step_type}' for step")
            return self.agents[step_type]

        # If no explicit tag or tag doesn't match an agent, use LLM to recommend
        recommended_agent_key = await self._get_step_executor_with_llm(step_info)

        # If LLM provided a valid recommendation, use it
        if recommended_agent_key and recommended_agent_key in self.agents:
            return self.agents[recommended_agent_key]

        # Fall back to default selection logic if LLM couldn't provide a valid recommendation
        for key in self.executor_keys:
            if key in self.agents:
                return self.agents[key]

        # Last resort: return primary agent
        return self.primary_agent

    async def execute(self, input_text: str) -> str:
        """Execute the simple planning with agents."""
        try:
            if not self.primary_agent:
                raise ValueError("No primary agent available")

            # Create initial plan if input provided
            if input_text:
                await self._create_initial_plan(input_text)

                # Verify plan was created successfully
                if self.active_plan_id not in self.planning_tool.plans:
                    logger.error(
                        f"Plan creation failed. Plan ID {self.active_plan_id} not found in planning tool."
                    )
                    return f"Failed to create plan for: {input_text}"

            result = ""
            while True:
                # Get current step to execute
                self.current_step_index, step_info = await self._get_current_step_info()

                # Exit if no more steps or plan completed
                if self.current_step_index is None:
                    result += await self._finalize_plan()
                    break

                # Execute current step with appropriate agent
                step_info = step_info if step_info else {"text": "Execute next task"}
                executor = await self.get_executor(step_info)
                step_result = await self._execute_step(executor, step_info)
                result += step_result + "\n"

                # Check if agent wants to terminate
                if hasattr(executor, "state") and executor.state == AgentState.FINISHED:
                    break

            return result
        except Exception as e:
            logger.error(f"Error in PlanningFlow: {str(e)}")
            return f"Execution failed: {str(e)}"

    async def _create_initial_plan(self, request: str) -> None:
        """Create an initial plan based on the request using the planning's LLM and PlanningTool."""
        logger.info(f"Creating initial plan with ID: {self.active_plan_id}")

        # Create a system message for plan creation with improved flexibility for task complexity
        system_message = Message.system_message(
            """You are an expert Planning Assistant tasked with solving problems efficiently through structured plans.

Your job is to analyze the task and create an appropriate plan that:
1. Matches the complexity of the task - use fewer steps for simple tasks, more for complex ones
2. Avoids over-decomposition - don't break simple tasks into too many steps
3. Captures the essential actions needed to complete the task successfully

GUIDELINES FOR PLAN CREATION:
- For simple tasks: Create 2-3 high-level steps that cover the entire workflow
- For medium complexity: Use 3-5 steps that capture key milestones
- For complex tasks: Use 5-7 steps maximum, focusing on critical decision points and major phases
- Each step should represent meaningful progress toward the goal
- Steps should be logically connected with clear dependencies
- Focus on outcomes rather than processes

IMPORTANT: Your response MUST be in the same language as the user's request."""
        )

        # Create a user message with the request
        user_message = Message.user_message(
            f"Analyze this task and create an appropriately sized plan: {request}"
        )

        # Call LLM with PlanningTool
        response = await self.llm.ask_tool(
            messages=[user_message],
            system_msgs=[system_message],
            tools=[self.planning_tool.to_param()],
            tool_choice=ToolChoice.AUTO,
        )

        # Process tool calls if present
        if response.tool_calls:
            for tool_call in response.tool_calls:
                if tool_call.function.name == "planning":
                    # Parse the arguments
                    args = tool_call.function.arguments
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except json.JSONDecodeError:
                            logger.error(f"Failed to parse tool arguments: {args}")
                            continue

                    # Ensure plan_id is set correctly and execute the tool
                    args["plan_id"] = self.active_plan_id

                    # Execute the tool via ToolCollection instead of directly
                    result = await self.planning_tool.execute(**args)

                    logger.info(f"Plan creation result: {str(result)}")
                    return

        # If execution reached here, create a default plan
        logger.warning("Creating default plan")

        # Detect language to provide appropriate default steps
        def has_cjk_chars(text):
            return any(ord(c) > 0x3000 for c in text)
        
        def has_cyrillic_chars(text):
            return any(0x0400 <= ord(c) <= 0x04FF for c in text)
        
        # Create language-appropriate default steps
        if has_cjk_chars(request):
            default_steps = ["分析需求", "执行任务", "验证结果"]
        elif has_cyrillic_chars(request):
            default_steps = ["Анализ запроса", "Выполнение задачи", "Проверка результатов"]
        else:
            default_steps = ["Analyze request", "Execute task", "Verify results"]

        # Create default plan using the ToolCollection
        await self.planning_tool.execute(
            **{
                "command": "create",
                "plan_id": self.active_plan_id,
                "title": f"Plan for: {request[:50]}{'...' if len(request) > 50 else ''}",
                "steps": default_steps,
            }
        )

    async def _get_current_step_info(self) -> tuple[Optional[int], Optional[dict]]:
        """
        Parse the current plan to identify the first non-completed step's index and info.
        Returns (None, None) if no active step is found.
        """
        if (
            not self.active_plan_id
            or self.active_plan_id not in self.planning_tool.plans
        ):
            logger.error(f"Plan with ID {self.active_plan_id} not found")
            return None, None

        try:
            # Direct access to plan data from planning tool storage
            plan_data = self.planning_tool.plans[self.active_plan_id]
            steps = plan_data.get("steps", [])
            step_statuses = plan_data.get("step_statuses", [])

            # Find first non-completed step
            for i, step in enumerate(steps):
                if i >= len(step_statuses):
                    status = PlanStepStatus.NOT_STARTED.value
                else:
                    status = step_statuses[i]

                if status in PlanStepStatus.get_active_statuses():
                    # Extract step type/category if available
                    step_info = {"text": step}

                    # Mark current step as in_progress
                    try:
                        await self.planning_tool.execute(
                            command="mark_step",
                            plan_id=self.active_plan_id,
                            step_index=i,
                            step_status=PlanStepStatus.IN_PROGRESS.value,
                        )
                    except Exception as e:
                        logger.warning(f"Error marking step as in_progress: {e}")
                        # Update step status directly if needed
                        if i < len(step_statuses):
                            step_statuses[i] = PlanStepStatus.IN_PROGRESS.value
                        else:
                            while len(step_statuses) < i:
                                step_statuses.append(PlanStepStatus.NOT_STARTED.value)
                            step_statuses.append(PlanStepStatus.IN_PROGRESS.value)

                        plan_data["step_statuses"] = step_statuses

                    return i, step_info

            return None, None  # No active step found

        except Exception as e:
            logger.warning(f"Error finding current step index: {e}")
            return None, None

    async def _execute_step(self, executor: BaseAgent, step_info: dict) -> str:
        """Execute the current step with the specified agent using agent.run()."""
        # Prepare context for the agent with current plan status
        plan_status = await self._get_plan_text()
        step_text = step_info.get("text", f"Step {self.current_step_index}")

        # Create a prompt for the agent to execute the current step
        step_prompt = f"""
        CURRENT PLAN STATUS:
        {plan_status}

        YOUR CURRENT TASK:
        You are now working on step {self.current_step_index}: "{step_text}"

        Please execute this step using the appropriate tools. When you're done, provide a summary of what you accomplished.
        """

        # Use agent.run() to execute the step
        try:
            step_result = await executor.run(step_prompt)

            # Mark the step as completed after successful execution
            await self._mark_step_completed()

            return step_result
        except Exception as e:
            logger.error(f"Error executing step {self.current_step_index}: {e}")
            return f"Error executing step {self.current_step_index}: {str(e)}"

    async def _mark_step_completed(self) -> None:
        """Mark the current step as completed."""
        if self.current_step_index is None:
            return

        try:
            # Mark the step as completed
            await self.planning_tool.execute(
                command="mark_step",
                plan_id=self.active_plan_id,
                step_index=self.current_step_index,
                step_status=PlanStepStatus.COMPLETED.value,
            )
            logger.info(
                f"Marked step {self.current_step_index} as completed in plan {self.active_plan_id}"
            )
        except Exception as e:
            logger.warning(f"Failed to update plan status: {e}")
            # Update step status directly in planning tool storage
            if self.active_plan_id in self.planning_tool.plans:
                plan_data = self.planning_tool.plans[self.active_plan_id]
                step_statuses = plan_data.get("step_statuses", [])

                # Ensure the step_statuses list is long enough
                while len(step_statuses) <= self.current_step_index:
                    step_statuses.append(PlanStepStatus.NOT_STARTED.value)

                # Update the status
                step_statuses[self.current_step_index] = PlanStepStatus.COMPLETED.value
                plan_data["step_statuses"] = step_statuses

    async def _get_plan_text(self) -> str:
        """Get the current plan as formatted text."""
        try:
            result = await self.planning_tool.execute(
                command="get", plan_id=self.active_plan_id
            )
            return result.output if hasattr(result, "output") else str(result)
        except Exception as e:
            logger.error(f"Error getting plan: {e}")
            return self._generate_plan_text_from_storage()

    def _generate_plan_text_from_storage(self) -> str:
        """Generate plan text directly from storage if the planning tool fails."""
        try:
            if self.active_plan_id not in self.planning_tool.plans:
                return f"Error: Plan with ID {self.active_plan_id} not found"

            plan_data = self.planning_tool.plans[self.active_plan_id]
            title = plan_data.get("title", "Untitled Plan")
            steps = plan_data.get("steps", [])
            step_statuses = plan_data.get("step_statuses", [])
            step_notes = plan_data.get("step_notes", [])

            # Ensure step_statuses and step_notes match the number of steps
            while len(step_statuses) < len(steps):
                step_statuses.append(PlanStepStatus.NOT_STARTED.value)
            while len(step_notes) < len(steps):
                step_notes.append("")

            # Count steps by status
            status_counts = {status: 0 for status in PlanStepStatus.get_all_statuses()}

            for status in step_statuses:
                if status in status_counts:
                    status_counts[status] += 1

            completed = status_counts[PlanStepStatus.COMPLETED.value]
            total = len(steps)
            progress = (completed / total) * 100 if total > 0 else 0

            plan_text = f"Plan: {title} (ID: {self.active_plan_id})\n"
            plan_text += "=" * len(plan_text) + "\n\n"

            plan_text += (
                f"Progress: {completed}/{total} steps completed ({progress:.1f}%)\n"
            )
            plan_text += f"Status: {status_counts[PlanStepStatus.COMPLETED.value]} completed, {status_counts[PlanStepStatus.IN_PROGRESS.value]} in progress, "
            plan_text += f"{status_counts[PlanStepStatus.BLOCKED.value]} blocked, {status_counts[PlanStepStatus.NOT_STARTED.value]} not started\n\n"
            plan_text += "Steps:\n"

            status_marks = PlanStepStatus.get_status_marks()

            for i, (step, status, notes) in enumerate(
                zip(steps, step_statuses, step_notes)
            ):
                # Use status marks to indicate step status
                status_mark = status_marks.get(
                    status, status_marks[PlanStepStatus.NOT_STARTED.value]
                )

                plan_text += f"{i}. {status_mark} {step}\n"
                if notes:
                    plan_text += f"   Notes: {notes}\n"

            return plan_text
        except Exception as e:
            logger.error(f"Error generating plan text from storage: {e}")
            return f"Error: Unable to retrieve plan with ID {self.active_plan_id}"

    async def _finalize_plan(self) -> str:
        """Finalize the plan and provide a summary using the planning's LLM directly."""
        plan_text = await self._get_plan_text()

        # Create a summary using the planning's LLM directly
        try:
            system_message = Message.system_message(
                "You are a planning assistant. Your task is to summarize the completed plan."
            )

            user_message = Message.user_message(
                f"The plan has been completed. Here is the final plan status:\n\n{plan_text}\n\nPlease provide a summary of what was accomplished and any final thoughts."
            )

            response = await self.llm.ask(
                messages=[user_message], system_msgs=[system_message]
            )

            return f"Plan completed:\n\n{response}"
        except Exception as e:
            logger.error(f"Error finalizing plan with LLM: {e}")

            # Fallback to using an agent for the summary
            try:
                agent = self.primary_agent
                summary_prompt = f"""
                The plan has been completed. Here is the final plan status:

                {plan_text}

                Please provide a summary of what was accomplished and any final thoughts.
                """
                summary = await agent.run(summary_prompt)
                return f"Plan completed:\n\n{summary}"
            except Exception as e2:
                logger.error(f"Error finalizing plan with agent: {e2}")
                return "Plan completed. Error generating summary."
