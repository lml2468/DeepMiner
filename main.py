import asyncio
import time

from app.agent.dataminer import DataMiner
from app.agent.swe import SWEAgent
from app.agent.web import WebAgent
from app.common.logger import logger
from app.planning.simple import SimplePlanner


async def run():
    agents = {
        "WebAgent": WebAgent(),
        "SWEAgent": SWEAgent(),
        "DataMiner": DataMiner(),
    }

    try:
        prompt = input("Enter your prompt: ")

        if prompt.strip().isspace() or not prompt:
            logger.warning("Empty prompt provided.")
            return

        planner = SimplePlanner(agents=agents)
        logger.warning("Processing your request...")

        try:
            start_time = time.time()
            result = await asyncio.wait_for(
                planner.execute(prompt),
                timeout=3600,  # 60 minute timeout for the entire execution
            )
            elapsed_time = time.time() - start_time
            logger.info(f"Request processed in {elapsed_time:.2f} seconds")
            logger.info(result)
        except asyncio.TimeoutError:
            logger.error("Request processing timed out after 1 hour")
            logger.info(
                "Operation terminated due to timeout. Please try a simpler request."
            )

    except KeyboardInterrupt:
        logger.info("Operation cancelled by user.")
    except Exception as e:
        logger.error(f"Error: {str(e)}")


if __name__ == "__main__":
    asyncio.run(run())