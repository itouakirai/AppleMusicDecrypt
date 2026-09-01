import asyncio
import subprocess
from typing import Dict, Optional

from creart import it

from src.api import WebAPI
from src.config import Config
from src.exceptions import CodecNotFoundException, SongNotPassIntegrityCheckException
from src.flags import Flags
from src.wrapper import WrapperManager
from src.legacy.decrypt import WidevineDecrypt
from src.legacy.mp4 import decrypt as legacy_decrypt
from src.legacy.mp4 import extract_media as legacy_extract_media
from src.logger import RipLogger
from src.measurer import Measurer
from src.metadata import SongMetadata
from src.models import PlaylistInfo
from src.mp4 import extract_media, extract_song, encapsulate, write_metadata, fix_encapsulate, fix_esds_box, \
    check_song_integrity
from src.save import save
from src.task import Task, Status
from src.types import Codec, ParentDoneHandler
from src.url import Song, Album, URLType, Playlist
from src.utils import get_codec_from_codec_id, check_song_existence, check_song_exists, if_raw_atmos, \
    check_album_existence, playlist_write_song_index, run_sync, safely_create_task, language_exist, query_language


class DownloadManager:
    def __init__(self):
        self.adam_id_task_mapping: Dict[str, Task] = {}
        self.task_lock = asyncio.Semaphore(it(Config).download.maxRunningTasks)

    async def register_task(self, task: Task):
        self.adam_id_task_mapping[task.adamId] = task
        await self.task_lock.acquire()
        it(Measurer).record_task_start()

    async def unregister_task(self, task: Task):
        if task.adamId in self.adam_id_task_mapping:
            del self.adam_id_task_mapping[task.adamId]
            self.task_lock.release()
            it(Measurer).record_task_finish()

    def get_task(self, adam_id: str) -> Optional[Task]:
        return self.adam_id_task_mapping.get(adam_id)


class Ripper:
    def __init__(self):
        self.download_manager = DownloadManager()

    async def rip_song(self, url: Song, codec: str, flags: Flags = Flags(),
                       parent_done: ParentDoneHandler = None, playlist: PlaylistInfo = None,
                       timeout_sec: int = 0):
        if self.download_manager.get_task(url.id):
            if parent_done:
                # If task already exists, we must notify the parent that this "sub-task" is considered handled/skipped
                # to prevent the parent from waiting indefinitely.
                await parent_done.try_done()
            return

        task = Task(adamId=url.id, parentDone=parent_done, playlist=playlist)

        # Initialize Logger
        task.logger = RipLogger(URLType.Song, task.adamId)

        try:
            await self.download_manager.register_task(task)

            # Fetch Metadata
            raw_metadata = await it(WebAPI).get_song_info(task.adamId, url.storefront, flags.language)
            album_data = await it(WebAPI).get_album_info(raw_metadata.relationships.albums.data[0].id, url.storefront,
                                                         flags.language)
            task.metadata = SongMetadata.parse_from_song_data(raw_metadata)
            task.metadata.parse_from_album_data(album_data)

            # Update Logger with metadata
            task.logger.set_fullname(task.metadata.artist, task.metadata.title)
            task.logger.create()

            # Check Language
            if it(Config).region.languageNotExistWarning and not language_exist(url.storefront, flags.language):
                default_language, _ = query_language(url.storefront)
                task.logger.language_not_exist(url.storefront, flags.language, default_language)

            # Check Existence on Apple Music
            if not await check_song_existence(url.id, url.storefront):
                task.logger.not_exist()
                task.update_status(Status.FAILED)
                task.error = Exception("Song not found on Apple Music")
                return

            # Get Cover and Lyrics
            task.metadata.cover = await it(WebAPI).get_cover(task.metadata.cover_url,
                                                             it(Config).download.coverFormat,
                                                             it(Config).download.coverSize)

            if raw_metadata.attributes.hasTimeSyncedLyrics:
                task.metadata.lyrics = await it(WrapperManager).lyrics(task.adamId, flags.language, url.storefront)

            if playlist:
                task.metadata.set_playlist_index(playlist.songIdIndexMapping.get(url.id))

            # Check Local Existence
            if not flags.force_save and check_song_exists(task.metadata, codec, playlist):
                task.logger.already_exist()
                task.update_status(Status.DONE)
                return

            # Get M3U8
            m3u8_url = await self._get_m3u8_url(task, codec, raw_metadata)

            if codec == Codec.AAC_LEGACY or (
                    it(Config).download.codecAlternative and not raw_metadata.attributes.extendedAssetUrls.enhancedHls and Codec.AAC_LEGACY in it(
                    Config).download.codecPriority):
                await self._rip_song_legacy(task, timeout_sec)
                return

            if not m3u8_url:
                task.logger.logger.error("Lossless audio does not exist")
                task.update_status(Status.FAILED)
                task.error = Exception("Lossless audio does not exist")
                return

            try:
                task.m3u8Info = await extract_media(m3u8_url, codec, task)
            except CodecNotFoundException:
                task.logger.audio_not_exist()
                task.update_status(Status.FAILED)
                task.error = CodecNotFoundException(f"Audio codec '{codec}' not found")
                return

            task.logger.selected_codec(task.m3u8Info.codec_id)
            if all([bool(task.m3u8Info.bit_depth), bool(task.m3u8Info.sample_rate)]):
                task.metadata.set_bit_depth_and_sample_rate(task.m3u8Info.bit_depth, task.m3u8Info.sample_rate)
                # Check existence again with precise metadata
                if not flags.force_save and check_song_exists(task.metadata, codec, playlist):
                    task.logger.already_exist()
                    task.update_status(Status.DONE)
                    return

            # Wait in queue
            task.logger.logger.info("Waiting for available download streams...")
            async with it(WebAPI).download_lock:
                async def _phase2():
                    # Download
                    task.logger.downloading()
                    task.update_status(Status.DOWNLOADING)
                    raw_song = await it(WebAPI)._download_song_internal(task.m3u8Info.uri)
        
                    # Decrypt
                    task.logger.decrypting()
                    task.update_status(Status.DECRYPTING)
        
                    task.info = await run_sync(extract_song, raw_song, get_codec_from_codec_id(task.m3u8Info.codec_id))
                    # Decrypt all samples locally with Temari using keys from wrapper-lite /key
                    decrypted_samples = await self._decrypt_with_temari(task.adamId, task.m3u8Info, task.info.samples)
        
                    local_codec = get_codec_from_codec_id(task.m3u8Info.codec_id)
        
                    song_bytes = await run_sync(encapsulate, task.info, bytes().join(decrypted_samples),
                                          it(Config).download.atmosConventToM4a)
                    if not if_raw_atmos(local_codec, it(Config).download.atmosConventToM4a):
                        if local_codec != Codec.EC3 and local_codec != Codec.AC3:
                            song_bytes = await run_sync(fix_encapsulate, song_bytes)
                        song_bytes = await run_sync(write_metadata, song_bytes, task.metadata, it(Config).metadata.embedMetadata,
                                              it(Config).download.coverFormat, task.info.params)
                        if local_codec == Codec.AAC or local_codec == Codec.AAC_DOWNMIX or local_codec == Codec.AAC_BINAURAL:
                            song_bytes = await run_sync(fix_esds_box, task.info.raw, song_bytes)
        
                    if not await run_sync(check_song_integrity, song_bytes):
                        if it(Config).download.failedSongNotPassIntegrityCheck:
                            task.logger.failed_integrity(True)
                            task.update_status(Status.FAILED)
                            raise SongNotPassIntegrityCheckException("Integrity Check Failed")
                        else:
                            task.logger.failed_integrity(False)
                            task.error = SongNotPassIntegrityCheckException("Integrity Check Warning")
        
                    local_filename = await run_sync(save, song_bytes, local_codec, task.metadata, task.playlist)
                    task.logger.saved()
                    task.update_status(Status.DONE)
        
                    if it(Config).download.afterDownloaded:
                        command = it(Config).download.afterDownloaded.format(filename=local_filename)
                        subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                
                if timeout_sec > 0:
                    await asyncio.wait_for(_phase2(), timeout=timeout_sec)
                else:
                    await _phase2()

        except asyncio.TimeoutError:
            task.logger.logger.warning("Task processing timed out after waiting in queue")
            task.update_status(Status.FAILED)
            task.error = Exception("Task execution timed out")

        except Exception as e:
            task.logger.logger.exception(f"Error processing song: {e}")
            task.update_status(Status.FAILED)
            task.error = e
        except asyncio.CancelledError:
            task.logger.logger.warning("Task processing timed out or was cancelled")
            task.update_status(Status.FAILED)
            task.error = Exception("Task execution timed out")
            raise
        finally:
            await self.download_manager.unregister_task(task)
            task.update_status(task.status)  # Ensure status is set
            if task.parentDone:
                await task.parentDone.try_done()

    async def _get_m3u8_url(self, task: Task, codec: str, raw_metadata) -> Optional[str]:
        if not raw_metadata.attributes.extendedAssetUrls:
            task.logger.audio_not_exist()
            return None

        m3u8_url = None
        if codec == Codec.ALAC and raw_metadata.attributes.extendedAssetUrls.enhancedHls:
            m3u8_url = await it(WrapperManager).m3u8(task.adamId)
        else:
            if codec != Codec.AAC_LEGACY:
                m3u8_url = raw_metadata.attributes.extendedAssetUrls.enhancedHls

        return m3u8_url

    async def _rip_song_legacy(self, task: Task, timeout_sec: int = 0):
        # Simplified legacy ripping integrated into the flow
        try:
            task.m3u8Info = await legacy_extract_media(await it(WrapperManager).webPlayback(task.adamId))

            async with it(WebAPI).download_lock:
                async def _phase2():
                    task.logger.downloading()
                    task.update_status(Status.DOWNLOADING)
                    raw_song = await it(WebAPI)._download_song_internal(task.m3u8Info.uri)
                    task.info = await run_sync(extract_song, raw_song, Codec.AAC_LEGACY)
                    
                    task.logger.decrypting()
                    task.update_status(Status.DECRYPTING)
                    wvDecrypt = WidevineDecrypt()
                    challenge = wvDecrypt.generate_challenge(task.m3u8Info.keys[0].split(",")[1])
                    wvLicense = await it(WrapperManager).license(adam_id=task.adamId, challenge=challenge,
                                                                 kid=task.m3u8Info.keys[0])
                    keys = wvDecrypt.generate_key(wvLicense)
                    song_bytes = await run_sync(legacy_decrypt, raw_song, keys[1].kid.hex, keys[1].key.hex())
        
                    song_bytes = await run_sync(write_metadata, song_bytes, task.metadata, it(Config).metadata.embedMetadata,
                                          it(Config).download.coverFormat, task.info.params)
        
                    if not await run_sync(check_song_integrity, song_bytes):
                        task.logger.failed_integrity(True)
        
                    local_filename = await run_sync(save, song_bytes, Codec.AAC_LEGACY, task.metadata, task.playlist)
                    task.logger.saved()
                    task.update_status(Status.DONE)
        
                    if it(Config).download.afterDownloaded:
                        command = it(Config).download.afterDownloaded.format(filename=local_filename)
                        subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

                if timeout_sec > 0:
                    await asyncio.wait_for(_phase2(), timeout=timeout_sec)
                else:
                    await _phase2()

        except asyncio.TimeoutError:
            task.logger.logger.warning("Task processing timed out after waiting in queue")
            task.update_status(Status.FAILED)
            task.error = Exception("Legacy Task execution timed out")
        except Exception as e:
            task.logger.logger.exception(f"Legacy rip failed: {e}")
            task.update_status(Status.FAILED)
            task.error = e
            raise e

    async def rip_album(self, url: Album, codec: str, flags: Flags = Flags(), parent_done: ParentDoneHandler = None):
        album_info = await it(WebAPI).get_album_info(url.id, url.storefront, flags.language)
        logger = RipLogger(url.type, url.id)
        logger.set_fullname(album_info.data[0].attributes.artistName, album_info.data[0].attributes.name)

        logger.create()
        if not await check_album_existence(url.id, url.storefront):
            logger.not_exist()
            return

        async def on_children_done():
            logger.done()
            if parent_done:
                await parent_done.try_done()

        done_handler = ParentDoneHandler(len(album_info.data[0].relationships.tracks.data), on_children_done)

        for track in album_info.data[0].relationships.tracks.data:
            song = Song(id=track.id, storefront=url.storefront, url="", type=URLType.Song)
            safely_create_task(self.rip_song(song, codec, flags, done_handler))

    async def rip_artist(self, url: Album, codec: str, flags: Flags = Flags()):
        artist_info = await it(WebAPI).get_artist_info(url.id, url.storefront, flags.language)
        logger = RipLogger(url.type, url.id)
        logger.set_fullname(artist_info.data[0].attributes.name)

        logger.create()

        async def on_children_done():
            logger.done()

        if flags.include_participate_in_works:
            songs = await it(WebAPI).get_songs_from_artist(url.id, url.storefront, flags.language)
            done_handler = ParentDoneHandler(len(songs), on_children_done)
            for song_url in songs:
                safely_create_task(self.rip_song(Song.parse_url(song_url), codec, flags, done_handler))
        else:
            albums = await it(WebAPI).get_albums_from_artist(url.id, url.storefront, flags.language)
            done_handler = ParentDoneHandler(len(albums), on_children_done)
            for album_url in albums:
                safely_create_task(self.rip_album(Album.parse_url(album_url), codec, flags, done_handler))

    async def rip_playlist(self, url: Playlist, codec: str, flags: Flags = Flags()):
        playlist_info = await it(WebAPI).get_playlist_info_and_tracks(url.id, url.storefront, flags.language)
        playlist_info = playlist_write_song_index(playlist_info)
        logger = RipLogger(url.type, url.id)
        logger.set_fullname(playlist_info.data[0].attributes.curatorName, playlist_info.data[0].attributes.name)

        logger.create()

        async def on_children_done():
            logger.done()

        done_handler = ParentDoneHandler(len(playlist_info.data[0].relationships.tracks.data), on_children_done)

        for track in playlist_info.data[0].relationships.tracks.data:
            song = Song(id=track.id, storefront=url.storefront, url="", type=URLType.Song)
            safely_create_task(self.rip_song(song, codec, flags, done_handler, playlist=playlist_info))

    async def _decrypt_with_temari(self, adam_id: str, m3u8_info, samples) -> list[bytes]:
        """Decrypt samples with Temari, fetching key templates from wrapper-lite /key."""
        import json
        from collections import defaultdict

        from temari import Temari

        groups: dict[str, list[tuple[int, bytes]]] = defaultdict(list)
        for i, sample in enumerate(samples):
            groups[m3u8_info.keys[sample.descIndex]].append((i, sample.data))

        result: list[bytes] = [b""] * len(samples)
        for key_uri, group in groups.items():
            key_data = await it(WrapperManager).key(adam_id, key_uri)
            template_json = json.dumps(key_data)
            plains = await run_sync(self._temari_decrypt, template_json, [data for _, data in group])
            for (i, _), plain in zip(group, plains):
                result[i] = plain
                it(Measurer).record_decrypt(len(plain))
        return result

    @staticmethod
    def _temari_decrypt(template_json: str, samples: list[bytes]) -> list[bytes]:
        from temari import Temari
        with Temari.from_json(template_json) as t:
            return t.decrypt_par(samples)
