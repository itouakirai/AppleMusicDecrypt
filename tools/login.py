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


async def on_2fa(username: str, password: str):
    two_step_code = input("2FA code: ")
    return int(two_step_code)


async def main():
    await it(WrapperManager).init(it(Config).instance)
    username = input("Username: ")
    password = input("Password: ")
    try:
        await it(WrapperManager).login(username, password, on_2fa)
    except WrapperManagerException as e:
        it(GlobalLogger).logger.error("Login Failed!")
        return
    it(GlobalLogger).logger.info("Login Success!")

if __name__ == '__main__':
    loop.run_until_complete(main())
