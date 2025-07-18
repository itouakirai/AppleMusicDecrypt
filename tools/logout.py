import asyncio
import os
import sys

from creart import add_creator, it

loop = asyncio.new_event_loop()
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.logger import LoggerCreator, GlobalLogger

add_creator(LoggerCreator)
from src.config import ConfigCreator, Config

add_creator(ConfigCreator)
from src.grpc.manager import WMCreator, WrapperManager, WrapperManagerException

add_creator(WMCreator)


async def main():
    await it(WrapperManager).init(it(Config).instance.url, it(Config).instance.secure)
    username = input("Username: ")
    try:
        await it(WrapperManager).logout(username)
    except WrapperManagerException as e:
        it(GlobalLogger).logger.error("Logout Failed!")
        return
    it(GlobalLogger).logger.info("Logout Success!")

if __name__ == '__main__':
    loop.run_until_complete(main())
