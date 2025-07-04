from collections.abc import Iterator
from typing import TYPE_CHECKING, Any, Optional

import numpy as np
import pyarrow as pa
from ray.data._internal.util import _check_import, call_with_retry
from ray.data.block import BlockMetadata
from ray.data.context import DataContext
from ray.data.datasource import Datasource
from ray.data.datasource.datasource import ReadTask

if TYPE_CHECKING:
    import lance


class LanceDatasource(Datasource):
    """Lance datasource, for reading Lance dataset."""

    # Errors to retry when reading Lance fragments.
    READ_FRAGMENTS_ERRORS_TO_RETRY = ["LanceError(IO)"]
    # Maximum number of attempts to read Lance fragments.
    READ_FRAGMENTS_MAX_ATTEMPTS = 10
    # Maximum backoff seconds between attempts to read Lance fragments.
    READ_FRAGMENTS_RETRY_MAX_BACKOFF_SECONDS = 32

    def __init__(
        self,
        uri: str,
        columns: Optional[list[str]] = None,
        filter: Optional[str] = None,
        storage_options: Optional[dict[str, str]] = None,
        scanner_options: Optional[dict[str, Any]] = None,
    ):
        _check_import(self, module="lance", package="pylance")

        import lance

        self.uri = uri
        self.scanner_options = scanner_options or {}
        if columns is not None:
            self.scanner_options["columns"] = columns
        if filter is not None:
            self.scanner_options["filter"] = filter
        self.storage_options = storage_options
        self.lance_ds: lance.LanceDataset = lance.dataset(uri=uri, storage_options=storage_options)

        match = []
        match.extend(self.READ_FRAGMENTS_ERRORS_TO_RETRY)
        match.extend(DataContext.get_current().retried_io_errors)
        self._retry_params = {
            "description": "read lance fragments",
            "match": match,
            "max_attempts": self.READ_FRAGMENTS_MAX_ATTEMPTS,
            "max_backoff_s": self.READ_FRAGMENTS_RETRY_MAX_BACKOFF_SECONDS,
        }

    def get_read_tasks(self, parallelism: int) -> list[ReadTask]:
        read_tasks = []
        for fragments in np.array_split(self.lance_ds.get_fragments(), parallelism):
            if len(fragments) <= 0:
                continue

            fragment_ids = [f.metadata.id for f in fragments]
            num_rows = sum(f.count_rows() for f in fragments)
            input_files = [
                data_file.path for f in fragments for data_file in f.data_files()
            ]

            # TODO(chengsu): Take column projection into consideration for schema.
            metadata = BlockMetadata(
                num_rows=num_rows,
                schema=fragments[0].schema,
                input_files=input_files,
                size_bytes=None,
                exec_stats=None,
            )

            # Create bound variables to avoid loop variable binding issues
            def create_read_task(
                fragment_ids: list[int],
                lance_ds: "lance.LanceDataset",
                scanner_options: dict[str, Any],
                retry_params: dict[str, Any],
                metadata: BlockMetadata
            ) -> ReadTask:
                return ReadTask(
                    lambda: _read_fragments_with_retry(
                        fragment_ids,
                        lance_ds,
                        scanner_options,
                        retry_params,
                    ),
                    metadata,
                )

            read_task = create_read_task(
                fragment_ids,
                self.lance_ds,
                self.scanner_options,
                self._retry_params,
                metadata,
            )
            read_tasks.append(read_task)

        return read_tasks

    def estimate_inmemory_data_size(self) -> Optional[int]:
        # TODO(chengsu): Add memory size estimation to improve auto-tune of parallelism.
        return None


def _read_fragments_with_retry(
    fragment_ids: list[int],
    lance_ds: "lance.LanceDataset",
    scanner_options: dict[str, Any],
    retry_params: dict[str, Any],
) -> Iterator[pa.Table]:
    return call_with_retry(
        lambda: _read_fragments(fragment_ids, lance_ds, scanner_options),
        **retry_params,
    )


def _read_fragments(
    fragment_ids: list[int],
    lance_ds: "lance.LanceDataset",
    scanner_options: dict[str, Any],
) -> Iterator[pa.Table]:
    """Read Lance fragments in batches.

    NOTE: Use fragment ids, instead of fragments as parameter, because pickling
    LanceFragment is expensive.
    """
    fragments = [lance_ds.get_fragment(id) for id in fragment_ids]
    scanner_options["fragments"] = fragments
    scanner = lance_ds.scanner(**scanner_options)
    for batch in scanner.to_reader():
        yield pa.Table.from_batches([batch])
