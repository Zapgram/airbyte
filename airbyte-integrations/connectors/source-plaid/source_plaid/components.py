import inspect
import logging
import typing
import itertools

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

import requests

from airbyte_cdk.sources.declarative.decoders.decoder import Decoder
from airbyte_cdk.sources.declarative.decoders.json_decoder import JsonDecoder
from airbyte_cdk.sources.declarative.extractors.record_extractor import RecordExtractor


from dataclasses import InitVar, dataclass, field
from typing import Any, Iterable, List, Mapping, MutableMapping, Optional, Union


from airbyte_cdk.models import SyncMode
from airbyte_cdk.sources.connector_state_manager import ConnectorStateManager
from airbyte_cdk.sources.declarative.declarative_stream import DeclarativeStream
from airbyte_cdk.sources.declarative.interpolation import InterpolatedString
from airbyte_cdk.sources.declarative.migrations.state_migration import StateMigration
from airbyte_cdk.sources.declarative.retrievers.simple_retriever import SimpleRetriever
from airbyte_cdk.sources.declarative.schema import DefaultSchemaLoader
from airbyte_cdk.sources.declarative.schema.schema_loader import SchemaLoader
from airbyte_cdk.sources.streams.core import StreamData, NO_CURSOR_STATE_KEY
from airbyte_cdk.sources.types import Config, StreamSlice
from airbyte_cdk.models import AirbyteMessage, AirbyteStream, ConfiguredAirbyteStream, SyncMode
from airbyte_cdk.models import Type as MessageType
from airbyte_cdk.sources.streams.checkpoint import (
    CheckpointMode,
    CheckpointReader,
    FullRefreshCheckpointReader,
    IncrementalCheckpointReader,
    ResumableFullRefreshCheckpointReader,
)
from airbyte_cdk.sources.utils.schema_helpers import InternalConfig, ResourceSchemaLoader
from airbyte_cdk.sources.utils.slice_logger import SliceLogger
from airbyte_cdk.sources.utils.transform import TransformConfig, TypeTransformer


@dataclass
class PlaidTransactionExtractor(RecordExtractor):
    """
    Record extractor that searches a decoded response over multiple paths. Each field is
    a separate path that points to an array (unless it doesn't exist at all).

    If the field path points to an array, that array is returned.
    If the field path points to a non-existing path, an empty array is returned.

    Example of instantiating this transform:
    ```
      extractor:
        type: CustomRecordExtractor
        class_name: source_plaid.components.PlaidTransactionExtractor
        field_path:
          - "added"
          - "modified"
    ```

    Attributes:
        field_path (list[str]): Path to the fields that should be extracted
        decoder (Decoder): The decoder responsible to transfom the response in a Mapping
    """

    field_path: list[str]
    decoder: Decoder = field(default_factory=lambda: JsonDecoder(parameters={}))

    def extract_records(self, response: requests.Response) -> Iterable[Mapping[str, Any]]:
        body = self.decoder.decode(response)
        for path in self.field_path:
            extracted = body.get(path, [])
            yield from extracted


@dataclass
class PlaidRetriever(SimpleRetriever):
    state: MutableMapping[str, Any]

    @property
    def next_cursor(self) -> Optional[str]:
        return self._last_response.json().get("next_cursor")

    def read_records(
        self,
        records_schema: Mapping[str, Any],
        stream_slice: Optional[StreamSlice] = None,
    ) -> Iterable[StreamData]:
        """
        Fetch a stream's records from an HTTP API source

        :param records_schema: json schema to describe record
        :param stream_slice: The stream slice to read data for
        :return: The records read from the API source
        """
        _slice = stream_slice or StreamSlice(partition={}, cursor_slice={})  # None-check
        # Fixing paginator types has a long tail of dependencies
        self._paginator.reset()

        most_recent_record_from_slice = None
        record_generator = partial(
            self._parse_records,
            stream_state=self.state or {},
            stream_slice=_slice,
            records_schema=records_schema,
        )
        for stream_data in self._read_pages(record_generator, self.state, _slice):
            current_record = self._extract_record(stream_data, _slice)
            if self.cursor and current_record:
                self.cursor.observe(_slice, current_record)

            # Latest record read, not necessarily within slice boundaries.
            # TODO Remove once all custom components implement `observe` method.
            # https://github.com/airbytehq/airbyte-internal-issues/issues/6955
            most_recent_record_from_slice = self._get_most_recent_record(most_recent_record_from_slice, current_record, _slice)
            yield stream_data

        self.state["next_cursor"] = self.next_cursor

        if self.cursor:
            self.cursor.close_slice(_slice, most_recent_record_from_slice)
        return


# @dataclass
# class PlaidDeclarativeStream(DeclarativeStream):
#     """
#     DeclarativeStream is a Stream that delegates most of its logic to its schema_load and retriever

#     Attributes:
#         name (str): stream name
#         primary_key (Optional[Union[str, List[str], List[List[str]]]]): the primary key of the stream
#         schema_loader (SchemaLoader): The schema loader
#         retriever (Retriever): The retriever
#         config (Config): The user-provided configuration as specified by the source's spec
#         stream_cursor_field (Optional[Union[InterpolatedString, str]]): The cursor field
#         stream. Transformations are applied in the order in which they are defined.
#     """

#     retriever: PlaidRetriever

#     def read(
#         self,
#         configured_stream: ConfiguredAirbyteStream,
#         logger: logging.Logger,
#         slice_logger: SliceLogger,
#         stream_state: MutableMapping[str, Any],
#         state_manager: ConnectorStateManager,
#         internal_config: InternalConfig,
#     ) -> Iterable[StreamData]:
#         sync_mode = configured_stream.sync_mode
#         cursor_field = configured_stream.cursor_field

#         # WARNING: When performing a read() that uses incoming stream state, we MUST use the self.state that is defined as
#         # opposed to the incoming stream_state value. Because some connectors like ones using the file-based CDK modify
#         # state before setting the value on the Stream attribute, the most up-to-date state is derived from Stream.state
#         # instead of the stream_state parameter. This does not apply to legacy connectors using get_updated_state().
#         try:
#             stream_state = self.state  # type: ignore # we know the field might not exist...
#         except AttributeError:
#             pass

#         checkpoint_reader = self._get_checkpoint_reader(
#             logger=logger, cursor_field=cursor_field, sync_mode=sync_mode, stream_state=stream_state
#         )

#         next_slice = checkpoint_reader.next()
#         record_counter = 0
#         while next_slice is not None:
#             if slice_logger.should_log_slice_message(logger):
#                 yield slice_logger.create_slice_log_message(next_slice)
#             records = self.read_records(
#                 sync_mode=sync_mode,  # todo: change this interface to no longer rely on sync_mode for behavior
#                 stream_slice=next_slice,
#                 stream_state=stream_state,
#                 cursor_field=cursor_field or None,
#             )
#             for record_data_or_message in records:
#                 yield record_data_or_message
#                 if isinstance(record_data_or_message, Mapping) or (
#                     hasattr(record_data_or_message, "type") and record_data_or_message.type == MessageType.RECORD
#                 ):
#                     record_data = record_data_or_message if isinstance(record_data_or_message, Mapping) else record_data_or_message.record

#                     # Thanks I hate it. RFR fundamentally doesn't fit with the concept of the legacy Stream.get_updated_state()
#                     # method because RFR streams rely on pagination as a cursor. Stream.get_updated_state() was designed to make
#                     # the CDK manage state using specifically the last seen record. don't @ brian.lai
#                     #
#                     # Also, because the legacy incremental state case decouples observing incoming records from emitting state, it
#                     # requires that we separate CheckpointReader.observe() and CheckpointReader.get_checkpoint() which could
#                     # otherwise be combined.
#                     if self.cursor_field:
#                         # Some connectors have streams that implement get_updated_state(), but do not define a cursor_field. This
#                         # should be fixed on the stream implementation, but we should also protect against this in the CDK as well
#                         self._observe_state(checkpoint_reader, self.get_updated_state(stream_state, record_data))
#                     record_counter += 1

#                     checkpoint_interval = self.state_checkpoint_interval
#                     checkpoint = checkpoint_reader.get_checkpoint()
#                     if checkpoint_interval and record_counter % checkpoint_interval == 0 and checkpoint is not None:
#                         airbyte_state_message = self._checkpoint_state(checkpoint, state_manager=state_manager)
#                         yield airbyte_state_message

#                     if internal_config.is_limit_reached(record_counter):
#                         break
#             self._observe_state(checkpoint_reader)
#             checkpoint_state = checkpoint_reader.get_checkpoint()
#             if checkpoint_state is not None:
#                 airbyte_state_message = self._checkpoint_state(checkpoint_state, state_manager=state_manager)
#                 yield airbyte_state_message

#             next_slice = checkpoint_reader.next()

#         checkpoint = checkpoint_reader.get_checkpoint()
#         if checkpoint.get(NO_CURSOR_STATE_KEY):
#             checkpoint = {"next_cursor": self.retriever.next_cursor}

#         if checkpoint is not None:
#             airbyte_state_message = self._checkpoint_state(checkpoint, state_manager=state_manager)
#             yield airbyte_state_message
