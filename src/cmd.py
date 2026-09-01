import argparse
import asyncio
import copy
import os
import sys

import httpx
from creart import it
from prompt_toolkit import PromptSession
from prompt_toolkit.completion import NestedCompleter
from prompt_toolkit.patch_stdout import patch_stdout

from src.api import WebAPI
from src.config import Config
from src.flags import Flags
from src.wrapper import WrapperManager, WrapperManagerException
from src.logger import GlobalLogger
from src.measurer import Measurer
from src.quality import print_song_quality, print_album_quality, print_playlist_quality, key_to_Headers
from src.rip import Ripper
from src.url import AppleMusicURL, URLType
from src.utils import check_dep, run_sync, safely_create_task, config_outdated


class InteractiveShell:
    loop: asyncio.AbstractEventLoop
    parser: argparse.ArgumentParser
    parser: argparse.ArgumentParser
    ripper: Ripper

    def __init__(self, loop: asyncio.AbstractEventLoop):
        self.ripper = Ripper()
        dep_installed, missing_dep = check_dep()
        if not dep_installed:
            it(GlobalLogger).logger.error(f"Dependence {missing_dep} was not installed!")
            loop.stop()
            sys.exit()

        self.loop = loop
        loop.run_until_complete(run_sync(it(WebAPI).init))
        loop.run_until_complete(it(WrapperManager).init(it(Config).instance.url))
        try:
            loop.run_until_complete(self.show_status())
        except (httpx.HTTPError, WrapperManagerException):
            it(GlobalLogger).logger.error("Unable to connect to the wrapper-lite instance")
            sys.exit()

        if config_outdated():
            it(GlobalLogger).logger.warning(
                "The configuration file is out of date. Please refer to config.example.toml to update it")

        self.parser = argparse.ArgumentParser(exit_on_error=False)
        subparser = self.parser.add_subparsers()
        download_parser = subparser.add_parser("download", aliases=["dl"])
        quality_parser = subparser.add_parser("quality", aliases=["qa"])
        download_parser.add_argument("url", nargs='*', type=str)
        download_parser.add_argument("-c", "--codec",
                                     choices=["alac", "ec3", "aac", "aac-binaural", "aac-downmix", "aac-legacy", "ac3"],
                                     default="alac")
        download_parser.add_argument("-f", "--force", default=False, action="store_true")
        download_parser.add_argument("-b", "--batch", default=False, action="store_true")
        download_parser.add_argument("-l", "--language", default=it(Config).region.language, action="store")
        download_parser.add_argument("--include-participate-songs", default=False, dest="include", action="store_true")

        quality_parser.add_argument("url", nargs='*', type=str)
        quality_parser.add_argument("-i", "--invert", default=False, action="store_true")
        quality_parser.add_argument("--codec-id", default=True, action="store_false")
        quality_parser.add_argument("--codec", default=True, action="store_false")
        quality_parser.add_argument("--bitrate", default=True, action="store_false")
        quality_parser.add_argument("--average-bitrate", default=True, action="store_false")
        quality_parser.add_argument("--channels", default=True, action="store_false")
        quality_parser.add_argument("--sample-rate", default=True, action="store_false")
        quality_parser.add_argument("--bit-depth", default=True, action="store_false")
        quality_parser.add_argument("-b", "--batch", default=False, action="store_true")

        subparser.add_parser("status")
        subparser.add_parser("exit")

        self.batch_mode = False

    async def show_status(self):
        it(WrapperManager).status.cache_invalidate()
        st_resp = await it(WrapperManager).status()
        if not st_resp.regions:
            it(GlobalLogger).logger.error(
                "The currently used wrapper-lite instance has no available account.")
        it(GlobalLogger).logger.info(f"Regions available on wrapper-lite instance: {', '.join(st_resp.regions)}")

    async def handle_batch_mode(self, args, cmds):
        try:
            if args.batch:
                self.batch_mode = True
                self.batch_args = args
                self.batch_command = cmds[0]
                it(GlobalLogger).logger.info(
                    "Entering batch mode. Enter one or more URLs per line (space-separated), type 'exit' to quit")
        except:
            pass

    async def batch_mode_parser(self, cmds: str):
        args = self.batch_args
        args.url = copy.deepcopy(cmds)
        if cmds[0] != "exit":
            cmds[0] = self.batch_command
        return cmds, args

    async def command_parser(self, cmd: str):
        if not cmd.strip():
            return
        cmds = cmd.split(" ")
        if self.batch_mode:
            cmds, args = await self.batch_mode_parser(cmds)
        else:
            try:
                args = self.parser.parse_args(cmds)
            except (argparse.ArgumentError, argparse.ArgumentTypeError, SystemExit):
                it(GlobalLogger).logger.warning(f"Unknown command: {cmd}")
                return
            await self.handle_batch_mode(args, cmds)
        match cmds[0]:
            case "download" | "dl":
                safely_create_task(self.do_download(args.url, args.codec, args.force, args.language, args.include))
            case "status":
                await self.show_status()
            case "exit":
                if self.batch_mode:
                    self.batch_mode = False
                    it(GlobalLogger).logger.info("Batch mode exited. Returning to normal command mode.")
                else:
                    self.handle_exit()
            case "quality" | "qa":
                safely_create_task(self.do_quality(args.url, args))

    async def do_download(self, raw_urls: list[str], codec: str, force_download: bool, language: str,
                          include: bool = False):
        for raw_url in raw_urls:
            url = AppleMusicURL.parse_url(raw_url)
            if not url:
                real_url = await it(WebAPI).get_real_url(raw_url)
                url = AppleMusicURL.parse_url(real_url)
                if not url:
                    it(GlobalLogger).logger.error(f"Illegal URL! - {raw_url}")
                    continue
            match url.type:
                case URLType.Song:
                    safely_create_task(
                        self.ripper.rip_song(url, codec, Flags(force_save=force_download, language=language)))
                case URLType.Album:
                    safely_create_task(
                        self.ripper.rip_album(url, codec, Flags(force_save=force_download, language=language)))
                case URLType.Artist:
                    safely_create_task(
                        self.ripper.rip_artist(url, codec, Flags(force_save=force_download, language=language,
                                                                 include_participate_in_works=include)))
                case URLType.Playlist:
                    safely_create_task(
                        self.ripper.rip_playlist(url, codec, Flags(force_save=force_download, language=language)))
                case _:
                    it(GlobalLogger).logger.error(f"Unsupported URLType - {raw_url}")
                    continue

    async def do_quality(self, raw_urls: list[str], args):
        all_fields = list(key_to_Headers.keys())
        show_fields = []
        for field in all_fields:
            if args.invert:
                show_fields = [f for f in key_to_Headers if not getattr(args, f)]
            else:
                show_fields = [f for f in key_to_Headers if getattr(args, f)]

        for raw_url in raw_urls:
            url = AppleMusicURL.parse_url(raw_url)
            if not url:
                real_url = await it(WebAPI).get_real_url(raw_url)
                url = AppleMusicURL.parse_url(real_url)
                if not url:
                    it(GlobalLogger).logger.error(f"Illegal URL! - {raw_url}")
                    continue
            match url.type:
                case URLType.Song:
                    safely_create_task(print_song_quality(url, show_fields))
                case URLType.Album:
                    safely_create_task(print_album_quality(url, show_fields))
                case URLType.Playlist:
                    safely_create_task(print_playlist_quality(url, show_fields))
                case _:
                    it(GlobalLogger).logger.error(f"Unsupported URLType - {raw_url}")
                    continue

    def bottom_toolbar(self):
        return f"Download Speed: {it(Measurer).download_speed()}, Decrypt Speed: {it(Measurer).decrypt_speed()}, Tasks: {it(Measurer).tasks_count()}"

    def completer(self):
        mycompleter = {
            "dl": {
                "--batch": None,
                "--codec": {
                    "ec3": None,
                    "aac": None,
                    "alac": None,
                    "aac-binaural": None,
                    "aac-downmix": None,
                    "aac-legacy": None,
                    "ac3": None
                },
                "--force": None,
                "--language": {
                    "en-US": None,
                    "en-GB": None,
                    "zh-Hans-CN": None,
                    "zh-Hant-HK": None,
                    "zh-Hant-TW": None,
                    "ja": None,
                    "ko": None
                },
                "--include-participate-songs": None
            },
            "qa": {
                "--invert": None,
                "--codec-id": None,
                "--codec": None,
                "--bitrate": None,
                "--average-bitrate": None,
                "--channels": None,
                "--sample-rate": None,
                "--bit-depth": None,
                "--batch": None
            },
            "status": None,
            "exit": None
        }
        return NestedCompleter.from_nested_dict(mycompleter)

    def handle_exit(self):
        if it(Measurer).tasks_count() > 0:
            it(GlobalLogger).logger.info(
                "There is still {} tasks, do you really want to exit? (y/N)".format(it(Measurer).tasks_count()))
            response = input().strip().lower()
            if response != 'y':
                return
        it(GlobalLogger).logger.info("Exit.")
        self.loop.stop()
        os._exit(0)

    async def handle_command(self):
        session = PromptSession("> ", bottom_toolbar=self.bottom_toolbar, completer=self.completer(),
                                refresh_interval=1)

        while True:
            try:
                command = await session.prompt_async()
                if command.strip() == '':
                    continue
                else:
                    await self.command_parser(command)
            except (EOFError, KeyboardInterrupt):
                self.handle_exit()

    async def start(self):
        with patch_stdout():
            await self.handle_command()
