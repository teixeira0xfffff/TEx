"""Telegram Report Generator."""
import base64
import datetime
import logging
import re
import os
import time
import zipfile
import shutil
import hashlib

from configparser import ConfigParser
from operator import attrgetter
from typing import Dict, List, Optional, cast
from os.path import basename

import pytz

from jinja2 import Environment, FileSystemLoader, Template, select_autoescape
from sqlalchemy.engine import ChunkedIteratorResult, CursorResult
from telethon import TelegramClient

from TEx.core.dir_manager import DirectoryManagerUtils
from TEx.core.base_module import BaseModule
from TEx.database.telegram_group_database import (
    TelegramGroupDatabaseManager,
    TelegramMediaDatabaseManager,
    TelegramMessageDatabaseManager,
    TelegramUserDatabaseManager
    )

from TEx.models.database.telegram_db_model import (
    TelegramGroupOrmEntity,
    TelegramMediaOrmEntity,
    TelegramMessageOrmEntity,
    TelegramUserOrmEntity
    )
from TEx.models.facade.telegram_group_report_facade_entity import TelegramGroupReportFacadeEntity, \
    TelegramGroupReportFacadeEntityMapper
from TEx.models.facade.telegram_message_report_facade_entity import TelegramMessageReportFacadeEntity, \
    TelegramMessageReportFacadeEntityMapper

logger = logging.getLogger()


class TelegramReportGenerator(BaseModule):
    """Generate Report from Telegram Groups."""

    __USERS_RESOLUTION_CACHE: Dict = {}

    async def run(self, config: ConfigParser, args: Dict, data: Dict) -> None:
        """Execute Module."""
        if not args['report']:
            logger.info('\t\tModule is Not Enabled...')
            return

        # Check Report and Assets Folder
        report_root_folder: str = args['report_folder']
        assets_root_folder: str = f'{report_root_folder}/assets/'

        # Purge Report Folder
        if os.path.exists(report_root_folder):
            shutil.rmtree(report_root_folder)

        # Create Dir Structure
        DirectoryManagerUtils.ensure_dir_struct(report_root_folder)
        DirectoryManagerUtils.ensure_dir_struct(assets_root_folder)

        # Get Report Template
        env = Environment(
            loader=FileSystemLoader("report_templates"),
            autoescape=select_autoescape()
            )
        report_template: Template = env.get_template("default_report.html")
        index_template: Template = env.get_template("default_index.html")

        # Load Groups from DB
        db_groups: List[TelegramGroupOrmEntity] = TelegramGroupDatabaseManager.get_all_by_phone_number(
            args['target_phone_number'])
        logger.info(f'\t\tFound {len(db_groups)} Groups')

        # Map to Facade Entities
        groups: List[TelegramGroupReportFacadeEntity] = [
            TelegramGroupReportFacadeEntityMapper.create_from_dbentity(item)
            for item in db_groups
            ]

        # Filter Groups
        groups = self.__filter_groups(
            args=args,
            source=groups
            )

        # Process Each Group
        for group in groups:
            logger.info(f'\t\tProcessing "{group.title}" ({group.id})')
            await self.__draw_report(
                args=args,
                assets_root_folder=assets_root_folder,
                group=group,
                report_root_folder=report_root_folder,
                template=report_template
                )

        # Render Index
        await self.__draw_index(
            args=args,
            report_root_folder=report_root_folder,
            template=index_template,
            groups=groups
            )

    def __filter_groups(self, args: Dict, source: List[TelegramGroupReportFacadeEntity]) -> List[TelegramGroupReportFacadeEntity]:
        """Apply Filter on Gropus."""
        groups: List[TelegramGroupReportFacadeEntity] = []

        # Filter Groups
        if args['group_id'] != '*':
            target_group_ids: List = [int(group) for group in str(args['group_id']).split(',')]
            logger.info(f'\t\tFiltering Groups by {target_group_ids}')
            groups = list(filter(lambda x: len([tg for tg in target_group_ids if tg == x.id]) > 0, source))
            logger.info(f'\t\tFound {len(groups)} after filtering')

        else:
            groups.extend(source)

        # Sort Groups by Title
        return sorted(groups, key=attrgetter('title'))

    async def __draw_index(self, args: Dict, report_root_folder: str, template: Template, groups: List[TelegramGroupReportFacadeEntity]) -> None:
        """Draw Index Page."""
        group_filter: str = args['group_id']
        words_filter: Optional[str] = args['filter']

        # Generate Object to Render
        logger.info('\t\t\tRendering Index Page')
        output = template.render(
            groups=[group for group in groups if getattr(group, 'meta_message_count', 0) > 0],
            end=datetime.datetime.now(tz=pytz.UTC).strftime('%Y-%m-%d %H:%M:%S'),
            start=(datetime.datetime.now(tz=pytz.UTC) - datetime.timedelta(seconds=int(args['limit_days']) * 24 * 60 * 60)).strftime('%Y-%m-%d %H:%M:%S'),
            now=datetime.datetime.now(tz=pytz.UTC).strftime('%Y-%m-%d %H:%M:%S'),
            target_phone=args['target_phone_number'],
            groups_filter=group_filter if group_filter != '*' else 'All',
            words_filter=words_filter if words_filter else 'None'
            )

        with open(f'{report_root_folder}/index.html', 'wb') as file:
            file.write(output.encode('utf-8'))
            file.flush()
            file.close()

    async def __draw_report(self, args: Dict, assets_root_folder: str, group: TelegramGroupReportFacadeEntity, report_root_folder: str, template: Template) -> None:
        """Process the Report for a Single Group Chat."""
        # Download All Messages
        logger.info('\t\t\tRetrieving Messages')

        # Apply Date/Time Limits
        limit_days: int = int(args['limit_days'])
        limit_seconds: int = limit_days * 24 * 60 * 60

        db_messages: List[TelegramMessageOrmEntity] = TelegramMessageDatabaseManager.get_all_messages_from_group(
            group_id=group.id,
            order_by_desc=args['order_desc'],
            message_datetime_limit_seconds=limit_seconds
            )

        # Convert Messages to Report Facade Entity
        messages: List[TelegramMessageReportFacadeEntity] = [
            TelegramMessageReportFacadeEntityMapper.create_from_dbentity(item)
            for item in db_messages
            ]

        # Filter Messages
        logger.info('\t\t\tFiltering')
        filter_words: Optional[List[str]] = args['filter'].split(',') if args['filter'] else None
        messages = self.filter_messages(messages=messages, filter_words=filter_words, args=args)

        # if Has 0 Messages, Get Out
        if len(messages) == 0:
            return 0

        logger.info('\t\t\tProcessing Messages')

        # Generate Object to Render
        render_messages: List = await self.process_messages(
            messages=messages,
            assets_root_folder=assets_root_folder
            )

        logger.info('\t\t\tRendering')
        with open(f'{report_root_folder}/result_{group.group_username}_{group.id}.html', 'wb') as file:
            output = template.render(
                groupname=group.title,
                groupusername=group.group_username,
                messages=render_messages
                )
            file.write(output.encode('utf-8'))
            file.flush()
            file.close()

        # Add Meta in Group
        group.meta_message_count = len(render_messages)

    async def process_messages(self, messages: List[TelegramMessageReportFacadeEntity], assets_root_folder: str) -> List[TelegramMediaOrmEntity]:
        """Process Group Messages."""
        h_result: List = []

        # Process Each Message
        for message in messages:

            # Get the From Message User
            from_user: Optional[TelegramUserOrmEntity] = self.get_user(message.from_id)

            # Check if Append the Message on Previous Message OR Creates a New One
            is_user_bot: bool = from_user is not None and not from_user.is_bot
            not_has_media = message.media_id is None
            is_same_user: bool = len(h_result) > 0 and h_result[-1]['from_id'] == message.from_id and h_result[-1]['to_id'] == message.to_id

            if is_user_bot and is_same_user and not_has_media:

                # Attach to Previous Message
                h_result[-1]['message'] += '\r\n' + message.message

            else:

                # Process new Message
                entry: Dict = {
                    'id': message.id,
                    'date_time': message.date_time,
                    'from_id': message.from_id,
                    'to_id': message.to_id,
                    'message': message.message,
                    'meta_next': getattr(message, 'meta_next', None),
                    'meta_previous': getattr(message, 'meta_previous', None),
                    'to_from_information': self.render_to_from_message_info(message=message, from_user=from_user)
                    }

                # Process Media
                entry.update(await self.get_media(message=message, assets_root_folder=assets_root_folder))

                h_result.append(entry)

        return h_result

    async def get_media(self, message: TelegramMessageReportFacadeEntity, assets_root_folder: str) -> Dict:
        """Download Media and Return the Metadata."""
        media_file_name: Optional[str] = None
        media_mime_type: Optional[str] = None
        media_geo: Optional[str] = None
        media_title: Optional[str] = None

        # Check if Have Media
        if message.media_id:

            # Get Media from DB
            media: Optional[TelegramMediaOrmEntity] = TelegramMediaDatabaseManager.get_by_id(
                pk=message.media_id,
                group_id=message.group_id
                )

            if media:
                if media.mime_type == 'application/vnd.geo':
                    media_geo = media.title.replace('|', ',')
                else:

                    media_path: str = f'{assets_root_folder}{media.id}_{media.file_name}'

                    # Save only If Media do Not Exists
                    if not os.path.exists(media_path):

                        # Save into assets folder
                        with open(media_path, 'wb') as file:
                            if not media.b64_content:
                                file.write(''.encode())
                            else:
                                file.write(base64.b64decode(media.b64_content))
                            file.flush()
                            file.close()

                    media_file_name = f'assets/{media.id}_{media.file_name}'
                    media_title = media.title

                media_mime_type = media.mime_type

                return {
                    'media_filename': media_file_name,
                    'media_mime_type': media_mime_type,
                    'media_geo': media_geo,
                    'media_title': media_title,
                    'media_is_image': media_mime_type and ('image/' in media_mime_type or media_mime_type == 'photo')
                    }

        return {
            'media_filename': None,
            'media_mime_type': None,
            'media_geo': None,
            'media_title': None,
            'media_is_image': None
            }

    def render_to_from_message_info(self, message: TelegramMessageReportFacadeEntity, from_user: Optional[TelegramUserOrmEntity]) -> str:
        """Build and Return the TO/FROM Information for Message."""
        # Get Users
        to_user: Optional[TelegramUserOrmEntity] = self.get_user(message.to_id)

        to_from_information: str = ''
        if from_user:
            to_from_information += f'- ({from_user.username}) {from_user.first_name if from_user.first_name else ""} {from_user.last_name if from_user.last_name else ""}' if from_user else ''
        if to_user:
            to_from_information += f' in reply to ({to_user.username}) {to_user.first_name if to_user.first_name else ""} {to_user.last_name if to_user.last_name else ""}' if to_user else ''

        return to_from_information

    def get_user(self, user_id: int) -> Optional[TelegramUserOrmEntity]:
        """Return the User from DB Resolution."""
        if user_id not in TelegramReportGenerator.__USERS_RESOLUTION_CACHE:
            TelegramReportGenerator.__USERS_RESOLUTION_CACHE.update(
                {user_id: TelegramUserDatabaseManager.get_by_id(user_id)}
                )

        return cast(Optional[TelegramUserOrmEntity], TelegramReportGenerator.__USERS_RESOLUTION_CACHE[user_id])

    def filter_messages(self, messages: List[TelegramMessageReportFacadeEntity], filter_words: Optional[List[str]], args: Dict) -> List[TelegramMessageReportFacadeEntity]:
        """Filter Messages."""
        if not filter_words or len(filter_words) == 0:
            return messages

        h_messages: List[TelegramMessageReportFacadeEntity] = []
        h_result: List[TelegramMessageReportFacadeEntity] = []

        # Loop on Messages
        for message in messages:

            matched: bool = False
            new_message: TelegramMessageReportFacadeEntity = message

            # Process Each Filter
            for word in filter_words:

                # Check Filter
                if word.casefold() in message.raw.casefold():
                    new_message.message = self.ireplace(word, f'<span class="marker">{word}</span>', new_message.message)
                    matched = True

            if matched:
                h_messages.append(new_message)

        # Add the Around Messages
        for single_result in h_messages:

            single_result.meta_next = False
            single_result.meta_previous = False

            # Get The Next and Previous Messages
            previous_messages: List[TelegramMessageReportFacadeEntity] = self.get_previous_messages(target_id=single_result.id, messages=messages, count=int(args['around_messages']))
            next_messages: List[TelegramMessageReportFacadeEntity] = self.get_next_messages(target_id=single_result.id, messages=messages, count=int(args['around_messages']))

            # Place an Color Wrapper Around
            for item in previous_messages:
                item.meta_previous = True
                item.meta_next = False

            for item in next_messages:
                item.meta_next = True
                item.meta_previous = False

            h_result.extend(previous_messages)
            h_result.append(single_result)
            h_result.extend(next_messages)

        return self.dedup_messages(messages=h_result)

    def ireplace(self, old: str, repl: str, text: str) -> str:
        """Case Insensitive Replace."""
        return re.sub('(?i)' + re.escape(old), lambda m: repl, text)

    def get_previous_messages(self, target_id: int, messages: List[TelegramMessageReportFacadeEntity], count: int) -> List[TelegramMessageReportFacadeEntity]:
        """Return the (count) messages prior the (id) message."""
        if count == 0:
            return []

        target_ix: int = [messages.index(item) for item in messages if item.id == target_id][0]
        dest_ix: int = target_ix - count

        if dest_ix > 0:
            return messages[dest_ix:target_ix]

        return messages[0:target_ix]

    def get_next_messages(self, target_id: int, messages: List[TelegramMessageReportFacadeEntity], count: int) -> List[TelegramMessageReportFacadeEntity]:
        """Return the (count) messages after the (id) message."""
        if count == 0:
            return []

        target_ix: int = [messages.index(item) for item in messages if item.id == target_id][0]
        dest_ix: int = target_ix + count + 1

        if dest_ix <= len(messages):
            return messages[target_ix + 1:dest_ix]

        return messages[target_ix:]

    def dedup_messages(self, messages: List[TelegramMessageReportFacadeEntity]) -> List[TelegramMessageReportFacadeEntity]:
        """Deduplicate the Messages."""
        if len(messages) == 0:
            return []

        h_result: List[TelegramMessageReportFacadeEntity] = []

        for message in messages:
            if len(h_result) == 0 or message.id != h_result[-1].id:
                h_result.append(message)

        return h_result


class TelegramReportSentViaTelegram(BaseModule):
    """Sent the Report to a Telegram user."""

    __USERS_RESOLUTION_CACHE: Dict = {}

    async def run(self, config: ConfigParser, args: Dict, data: Dict) -> None:
        """Execute Module."""
        if not args['sent_report_telegram']:
            logger.info('\t\tModule is Not Enabled...')
            return

        # Check Report and Assets Folder
        report_root_folder: str = args['report_folder']

        # Create Report File Name
        attach_name: str = args['attachment_name'].replace('@@now@@', datetime.datetime.strftime(datetime.datetime.now(tz=pytz.UTC), '%y%m%d_%H%M%S')) + ".zip"
        report_filename: str = os.path.join(report_root_folder, attach_name)
        logger.info(f'\t\t\tTarget Report Filename: {report_filename}')

        # Create a Zip File
        logger.info('\t\t\tGenerating Report ZIP File')
        with zipfile.ZipFile(report_filename, 'w', compresslevel=9, compression=zipfile.ZIP_DEFLATED) as zipObj:
            # Iterate over all the files in directory
            for folderName, subfolders, filenames in os.walk(report_root_folder):
                for filename in filenames:
                    filePath = os.path.join(folderName, filename)

                    if filePath == report_filename:
                            continue

                    zipObj.write(filePath, os.path.join(basename(folderName), filename))

        # Sent via Telegram
        client: TelegramClient = data['telegram_client']
        receiver = await client.get_input_entity(args['destination_username'])

        # Sent Message
        logger.info('\t\t\tSending Message')
        await client.send_message(
                receiver,
                args['title'].replace(
                    '@@now@@',
                    datetime.datetime.strftime(datetime.datetime.now(tz=pytz.UTC), '%y-%m-%d %H:%M:%S')
                ).replace('\\n', '\n')
            )
        time.sleep(1)
        # Sent the Report
        await client.send_file(receiver, f'{report_root_folder}/{attach_name}')

        # Remove Report File
        os.remove(report_filename)


class TelegramExportTextGenerator(BaseModule):
    """Export Telegram Messages."""

    __USERS_RESOLUTION_CACHE: Dict = {}

    async def run(self, config: ConfigParser, args: Dict, data: Dict) -> None:
        """Execute Module."""
        if not args['export_text']:
            logger.info('\t\tModule is Not Enabled...')
            return

        # Check Report and Assets Folder
        report_root_folder: str = args['report_folder']
        assets_root_folder: str = f'{report_root_folder}/assets/'

        # Purge Report Folder
        if os.path.exists(report_root_folder):
            shutil.rmtree(report_root_folder)

        # Create Dir Structure
        DirectoryManagerUtils.ensure_dir_struct(report_root_folder)
        DirectoryManagerUtils.ensure_dir_struct(assets_root_folder)

        # Load Groups from DB
        db_groups: List[TelegramGroupOrmEntity] = TelegramGroupDatabaseManager.get_all_by_phone_number(
            args['target_phone_number'])
        logger.info(f'\t\tFound {len(db_groups)} Groups')

        # Map to Facade Entities
        groups: List[TelegramGroupReportFacadeEntity] = [
            TelegramGroupReportFacadeEntityMapper.create_from_dbentity(item)
            for item in db_groups
            ]

        # Filter Groups
        groups = self.__filter_groups(
            args=args,
            source=groups
            )

        # Process Each Group
        for group in groups:
            logger.info(f'\t\tProcessing "{group.title}" ({group.id})')
            await self.__export_data(
                args=args,
                group=group,
                report_root_folder=report_root_folder
                )

    def __filter_groups(self, args: Dict, source: List[TelegramGroupReportFacadeEntity]) -> List[TelegramGroupReportFacadeEntity]:
        """Apply Filter on Gropus."""
        groups: List[TelegramGroupReportFacadeEntity] = []

        # Filter Groups
        if args['group_id'] != '*':
            target_group_ids: List = [int(group) for group in str(args['group_id']).split(',')]
            logger.info(f'\t\tFiltering Groups by {target_group_ids}')
            groups = list(filter(lambda x: len([tg for tg in target_group_ids if tg == x.id]) > 0, source))
            logger.info(f'\t\tFound {len(groups)} after filtering')

        else:
            groups.extend(source)

        # Sort Groups by Title
        return sorted(groups, key=attrgetter('title'))

    async def __export_data(self, args: Dict, group: TelegramGroupReportFacadeEntity, report_root_folder: str) -> None:
        """Process the Export for a Single Group Chat."""
        # Download All Messages
        logger.info('\t\t\tRetrieving Messages')

        # Apply Date/Time Limits
        limit_days: int = int(args['limit_days'])
        limit_seconds: int = limit_days * 24 * 60 * 60

        db_messages: List[TelegramMessageOrmEntity] = TelegramMessageDatabaseManager.get_all_messages_from_group(
            group_id=group.id,
            order_by_desc=args['order_desc'],
            message_datetime_limit_seconds=limit_seconds
            )

        # Convert Messages to Report Facade Entity
        messages: List[TelegramMessageReportFacadeEntity] = [
            TelegramMessageReportFacadeEntityMapper.create_from_dbentity(item)
            for item in db_messages
            ]

        # Filter Messages
        logger.info('\t\t\tFiltering')
        filter_regexs: Optional[List[str]] = args['regex'].split(',') if args['regex'] else None
        messages = self.filter_messages(messages=messages, filter_regexs=filter_regexs, args=args)

        # if Has 0 Messages, Get Out
        if len(messages) == 0:
            return 0

        logger.info('\t\t\tRendering')
        with open(f'{report_root_folder}/result_{group.group_username}_{group.id}.txt', 'wb') as file:

            for message in messages:
                file.write(message.encode('utf-8'))
                file.write('\r\n'.encode('utf-8'))

            file.flush()
            file.close()

        # Add Meta in Group
        group.meta_message_count = len(messages)

    def filter_messages(self, messages: List[TelegramMessageReportFacadeEntity], filter_regexs: Optional[List[str]], args: Dict) -> List[TelegramMessageReportFacadeEntity]:
        """Filter Messages."""
        if not filter_regexs or len(filter_regexs) == 0:
            return messages

        h_messages: List[TelegramMessageReportFacadeEntity] = []

        # Compile all Regex
        compiled_regex = [re.compile(item, flags=re.IGNORECASE|re.MULTILINE) for item in filter_regexs]

        # Loop on Messages
        for message in messages:

            matched: bool = False
            new_message: TelegramMessageReportFacadeEntity = message

            # Process Each Filter
            for rgx in compiled_regex:
                matches = rgx.findall(message.raw)

                if len(matches) > 0:
                    h_messages.extend(matches)

        return h_messages

    def ireplace(self, old: str, repl: str, text: str) -> str:
        """Case Insensitive Replace."""
        return re.sub('(?i)' + re.escape(old), lambda m: repl, text)


class TelegramExportFileGenerator(BaseModule):
    """Export Telegram Files."""

    __USERS_RESOLUTION_CACHE: Dict = {}
    __HASH_CACHE: List[str] = []

    async def run(self, config: ConfigParser, args: Dict, data: Dict) -> None:
        """Execute Module."""
        if not args['export_file']:
            logger.info('\t\tModule is Not Enabled...')
            return

        # Check Report and Assets Folder
        report_root_folder: str = args['report_folder']
        assets_root_folder: str = f'{report_root_folder}/assets/'

        # Purge Report Folder
        if os.path.exists(report_root_folder):
            shutil.rmtree(report_root_folder)

        # Create Dir Structure
        DirectoryManagerUtils.ensure_dir_struct(report_root_folder)
        DirectoryManagerUtils.ensure_dir_struct(assets_root_folder)

        # Load Groups from DB
        db_groups: List[TelegramGroupOrmEntity] = TelegramGroupDatabaseManager.get_all_by_phone_number(
            args['target_phone_number'])
        logger.info(f'\t\tFound {len(db_groups)} Groups')

        # Map to Facade Entities
        groups: List[TelegramGroupReportFacadeEntity] = [
            TelegramGroupReportFacadeEntityMapper.create_from_dbentity(item)
            for item in db_groups
            ]

        # Filter Groups
        groups = self.__filter_groups(
            args=args,
            source=groups
            )

        # Process Each Group
        for group in groups:
            logger.info(f'\t\tProcessing "{group.title}" ({group.id})')
            await self.__export_data(
                args=args,
                group=group,
                report_root_folder=report_root_folder
                )

    def __filter_groups(self, args: Dict, source: List[TelegramGroupReportFacadeEntity]) -> List[TelegramGroupReportFacadeEntity]:
        """Apply Filter on Gropus."""
        groups: List[TelegramGroupReportFacadeEntity] = []

        # Filter Groups
        if args['group_id'] != '*':
            target_group_ids: List = [int(group) for group in str(args['group_id']).split(',')]
            logger.info(f'\t\tFiltering Groups by {target_group_ids}')
            groups = list(filter(lambda x: len([tg for tg in target_group_ids if tg == x.id]) > 0, source))
            logger.info(f'\t\tFound {len(groups)} after filtering')

        else:
            groups.extend(source)

        # Sort Groups by Title
        return sorted(groups, key=attrgetter('title'))

    async def __export_data(self, args: Dict, group: TelegramGroupReportFacadeEntity, report_root_folder: str) -> None:
        """Process the Export for a Single Group Chat."""
        # Get Medias
        logger.info('\t\t\tRetrieving Messages')

        # Apply Date/Time Limits
        limit_days: int = int(args['limit_days'])
        limit_seconds: int = limit_days * 24 * 60 * 60

        # Get Medias
        logger.info('\t\t\tLoading Medias')
        medias: ChunkedIteratorResult = TelegramMediaDatabaseManager.get_all_medias_from_group_and_mimetype(
            group_id=group.id,
            file_datetime_limit_seconds=limit_seconds,
            mime_type=args['mime_type']
        )

        # if Has 0 Messages, Get Out
        for media in medias.yield_per(1):

            # Calculate Filename
            file_name: str = f'G_{group.id}_MID_{media[0].id}_' + "".join(char for char in media[0].file_name if char not in "%:/,\\\n\t\r[]<>*?;")

            # Get Binary Content
            bin_content = base64.b64decode(media[0].b64_content)

            # Compute FileHash
            f_hash: str = hashlib.md5(bin_content).hexdigest()

            # Check if Hash alread Exists in this Session
            if f_hash in TelegramExportFileGenerator.__HASH_CACHE:
                logger.info(f'\t\t\tFile Already Write - Same Hash - ({file_name}) > ID: {media[0].id} at {media[0].date_time}')
                continue

            # Write
            logger.info(f'\t\t\tWriting - ({file_name}) > ID: {media[0].id} at {media[0].date_time}')

            with open(os.path.join(report_root_folder, file_name), 'wb') as file:
                file.write(bin_content)
                file.flush()
                file.close()

            # Update Hash Table
            TelegramExportFileGenerator.__HASH_CACHE.append(f_hash)