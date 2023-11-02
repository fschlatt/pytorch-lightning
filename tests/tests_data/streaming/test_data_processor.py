import os
import sys
from typing import Any, List
from unittest import mock

import numpy as np
import pytest
import torch
from lightning import seed_everything
from lightning.data.streaming import data_processor as data_processor_module
from lightning.data.streaming import functions
from lightning.data.streaming.cache import Cache
from lightning.data.streaming.data_processor import (
    DataChunkRecipe,
    DataProcessor,
    DataTransformRecipe,
    _associated_items_to_workers,
    _download_data_target,
    _remove_target,
    _upload_fn,
    _wait_for_file_to_exist,
)
from lightning.data.streaming.functions import map, optimize
from lightning_utilities.core.imports import RequirementCache

_PIL_AVAILABLE = RequirementCache("PIL")


@pytest.mark.skipif(condition=sys.platform == "win32", reason="Not supported on windows")
def test_upload_fn(tmpdir):
    input_dir = os.path.join(tmpdir, "input_dir")
    os.makedirs(input_dir, exist_ok=True)

    cache_dir = os.path.join(tmpdir, "cache_dir")
    os.makedirs(cache_dir, exist_ok=True)

    remote_output_dir = os.path.join(tmpdir, "remote_output_dir")
    os.makedirs(remote_output_dir, exist_ok=True)

    filepath = os.path.join(input_dir, "a.txt")

    with open(filepath, "w") as f:
        f.write("HERE")

    upload_queue = mock.MagicMock()

    paths = [filepath, None]

    def fn(*_, **__):
        value = paths.pop(0)
        if value is None:
            return value
        return value

    upload_queue.get = fn

    remove_queue = mock.MagicMock()

    assert os.listdir(remote_output_dir) == []

    _upload_fn(upload_queue, remove_queue, cache_dir, remote_output_dir)

    assert os.listdir(remote_output_dir) == ["a.txt"]


@pytest.mark.skipif(condition=sys.platform == "win32", reason="Not supported on windows")
def test_remove_target(tmpdir):
    input_dir = os.path.join(tmpdir, "input_dir")
    os.makedirs(input_dir, exist_ok=True)

    cache_dir = os.path.join(tmpdir, "cache_dir")
    os.makedirs(cache_dir, exist_ok=True)

    filepath = os.path.join(cache_dir, "a.txt")

    with open(filepath, "w") as f:
        f.write("HERE")

    filepath = os.path.join(input_dir, "a.txt")

    queue_in = mock.MagicMock()

    paths = [filepath, None]

    def fn(*_, **__):
        value = paths.pop(0)
        if value is None:
            return value
        return [value]

    queue_in.get = fn

    assert os.listdir(cache_dir) == ["a.txt"]

    _remove_target(input_dir, cache_dir, queue_in)

    assert os.listdir(cache_dir) == []


@pytest.mark.skipif(condition=sys.platform == "win32", reason="Not supported on windows")
def test_download_data_target(tmpdir):
    input_dir = os.path.join(tmpdir, "input_dir")
    os.makedirs(input_dir, exist_ok=True)

    remote_input_dir = os.path.join(tmpdir, "remote_input_dir")
    os.makedirs(remote_input_dir, exist_ok=True)

    cache_dir = os.path.join(tmpdir, "cache_dir")
    os.makedirs(cache_dir, exist_ok=True)

    filepath = os.path.join(remote_input_dir, "a.txt")

    with open(filepath, "w") as f:
        f.write("HERE")

    filepath = os.path.join(input_dir, "a.txt")

    with open(filepath, "w") as f:
        f.write("HERE")

    queue_in = mock.MagicMock()

    paths = [filepath, None]

    def fn(*_, **__):
        value = paths.pop(0)
        if value is None:
            return value
        return (0, [value])

    queue_in.get = fn

    queue_out = mock.MagicMock()
    _download_data_target(input_dir, remote_input_dir, cache_dir, queue_in, queue_out)

    assert queue_out.put._mock_call_args_list[0].args == (0,)
    assert queue_out.put._mock_call_args_list[1].args == (None,)

    assert os.listdir(cache_dir) == ["a.txt"]


@pytest.mark.skipif(condition=sys.platform == "win32", reason="Not supported on windows")
def test_wait_for_file_to_exist():
    import botocore

    s3 = mock.MagicMock()
    obj = mock.MagicMock()
    raise_error = [True, True, False]

    def fn(*_, **__):
        value = raise_error.pop(0)
        if value:
            raise botocore.exceptions.ClientError({"Error": {"Code": "404", "Message": "Not Found"}}, "HeadObject")
        return

    s3.head_object = fn

    _wait_for_file_to_exist(s3, obj, sleep_time=0.01)

    assert len(raise_error) == 0

    def fn(*_, **__):
        raise ValueError("HERE")

    s3.head_object = fn

    with pytest.raises(ValueError, match="HERE"):
        _wait_for_file_to_exist(s3, obj, sleep_time=0.01)


def test_broadcast_object(tmpdir, monkeypatch):
    data_processor = DataProcessor(name="dummy", input_dir=tmpdir)
    assert data_processor._broadcast_object("dummy") == "dummy"
    monkeypatch.setenv("DATA_OPTIMIZER_NUM_NODES", "2")
    monkeypatch.setattr(data_processor_module, "_distributed_is_initialized", lambda: True)
    torch_mock = mock.MagicMock()
    monkeypatch.setattr(data_processor_module, "torch", torch_mock)
    assert data_processor._broadcast_object("dummy") == "dummy"
    assert torch_mock.distributed.broadcast_object_list._mock_call_args.args == (["dummy"], 0)


def test_cache_dir_cleanup(tmpdir, monkeypatch):
    cache_dir = os.path.join(tmpdir, "dummy")
    cache_data_dir = os.path.join(tmpdir, "data", "dummy")
    os.makedirs(cache_dir, exist_ok=True)
    os.makedirs(cache_data_dir, exist_ok=True)

    with open(os.path.join(cache_dir, "a.txt"), "w") as f:
        f.write("Hello World !")

    with open(os.path.join(cache_data_dir, "b.txt"), "w") as f:
        f.write("Hello World !")

    assert os.listdir(cache_dir) == ["a.txt"]
    assert os.listdir(cache_data_dir) == ["b.txt"]

    data_processor = DataProcessor(name="dummy", input_dir=tmpdir)
    monkeypatch.setenv("DATA_OPTIMIZER_CACHE_FOLDER", str(tmpdir))
    data_processor._cleanup_cache()

    assert os.listdir(cache_dir) == []
    assert os.listdir(cache_data_dir) == []


def test_associated_items_to_workers(monkeypatch):
    _, workers_user_items = _associated_items_to_workers(1, range(105))
    assert workers_user_items == [range(0, 105)]

    _, workers_user_items = _associated_items_to_workers(2, range(105))
    assert workers_user_items == [range(0, 52), range(52, 105)]

    _, workers_user_items = _associated_items_to_workers(3, range(105))
    assert workers_user_items == [range(0, 35), range(35, 70), range(70, 105)]

    _, workers_user_items = _associated_items_to_workers(4, range(105))
    assert workers_user_items == [range(0, 26), range(26, 52), range(52, 78), range(78, 105)]

    monkeypatch.setenv("DATA_OPTIMIZER_NUM_NODES", "2")

    _, workers_user_items = _associated_items_to_workers(1, range(105))
    assert workers_user_items == [range(0, 52)]

    _, workers_user_items = _associated_items_to_workers(2, range(105))
    assert workers_user_items == [range(0, 26), range(26, 52)]

    _, workers_user_items = _associated_items_to_workers(3, range(105))
    assert workers_user_items == [range(0, 17), range(17, 34), range(34, 52)]

    _, workers_user_items = _associated_items_to_workers(4, range(105))
    assert workers_user_items == [range(0, 13), range(13, 26), range(26, 39), range(39, 52)]

    monkeypatch.setenv("DATA_OPTIMIZER_NODE_RANK", "1")

    _, workers_user_items = _associated_items_to_workers(1, range(105))
    assert workers_user_items == [range(52, 105)]

    _, workers_user_items = _associated_items_to_workers(2, range(105))
    assert workers_user_items == [range(52, 78), range(78, 105)]

    _, workers_user_items = _associated_items_to_workers(3, range(105))
    assert workers_user_items == [range(52, 69), range(69, 86), range(86, 105)]

    _, workers_user_items = _associated_items_to_workers(4, range(105))
    assert workers_user_items == [range(52, 65), range(65, 78), range(78, 91), range(91, 105)]

    monkeypatch.setenv("DATA_OPTIMIZER_NUM_NODES", "4")
    monkeypatch.setenv("DATA_OPTIMIZER_NODE_RANK", "0")

    _, workers_user_items = _associated_items_to_workers(1, range(105))
    assert workers_user_items == [range(0, 26)]

    _, workers_user_items = _associated_items_to_workers(2, range(105))
    assert workers_user_items == [range(0, 13), range(13, 26)]

    _, workers_user_items = _associated_items_to_workers(3, range(105))
    assert workers_user_items == [range(0, 8), range(8, 16), range(16, 26)]

    _, workers_user_items = _associated_items_to_workers(4, range(105))
    assert workers_user_items == [range(0, 6), range(6, 12), range(12, 18), range(18, 26)]

    monkeypatch.setenv("DATA_OPTIMIZER_NODE_RANK", "3")

    _, workers_user_items = _associated_items_to_workers(1, range(105))
    assert workers_user_items == [range(78, 105)]

    _, workers_user_items = _associated_items_to_workers(2, range(105))
    assert workers_user_items == [range(78, 91), range(91, 105)]

    _, workers_user_items = _associated_items_to_workers(3, range(105))
    assert workers_user_items == [range(78, 87), range(87, 96), range(96, 105)]

    _, workers_user_items = _associated_items_to_workers(4, range(105))
    assert workers_user_items == [range(78, 84), range(84, 90), range(90, 96), range(96, 105)]


class CustomDataChunkRecipe(DataChunkRecipe):
    def prepare_structure(self, input_dir: str) -> List[Any]:
        filepaths = self.listdir(input_dir)
        assert len(filepaths) == 30
        return filepaths

    def prepare_item(self, item):
        return item


@pytest.mark.parametrize("delete_cached_files", [True])
@pytest.mark.parametrize("fast_dev_run", [10])
@pytest.mark.skipif(condition=not _PIL_AVAILABLE or sys.platform == "win32", reason="Requires: ['pil']")
def test_data_processsor(fast_dev_run, delete_cached_files, tmpdir, monkeypatch):
    from PIL import Image

    imgs = []
    for i in range(30):
        np_data = np.random.randint(255, size=(28, 28), dtype=np.uint32)
        img = Image.fromarray(np_data).convert("L")
        imgs.append(img)
        img.save(os.path.join(tmpdir, f"{i}.JPEG"))

    home_dir = os.path.join(tmpdir, "home")
    cache_dir = os.path.join(tmpdir, "cache")
    monkeypatch.setenv("DATA_OPTIMIZER_HOME_FOLDER", home_dir)
    monkeypatch.setenv("DATA_OPTIMIZER_CACHE_FOLDER", cache_dir)
    data_processor = DataProcessor(
        name="dummy_dataset",
        input_dir=tmpdir,
        num_workers=2,
        remote_input_dir=tmpdir,
        delete_cached_files=delete_cached_files,
        fast_dev_run=fast_dev_run,
    )
    data_processor.run(CustomDataChunkRecipe(chunk_size=2))

    assert sorted(os.listdir(cache_dir)) == ["data", "dummy_dataset"]

    fast_dev_run_enabled_chunks = [
        "chunk-0-0.bin",
        "chunk-0-1.bin",
        "chunk-0-2.bin",
        "chunk-0-3.bin",
        "chunk-0-4.bin",
        "chunk-1-0.bin",
        "chunk-1-1.bin",
        "chunk-1-2.bin",
        "chunk-1-3.bin",
        "chunk-1-4.bin",
        "index.json",
    ]

    fast_dev_run_disabled_chunks = [
        "chunk-0-0.bin",
        "chunk-0-1.bin",
        "chunk-0-2.bin",
        "chunk-0-3.bin",
        "chunk-0-4.bin",
        "chunk-0-5.bin",
        "chunk-0-6.bin",
        "chunk-0-7.bin",
        "chunk-1-0.bin",
        "chunk-1-1.bin",
        "chunk-1-2.bin",
        "chunk-1-3.bin",
        "chunk-1-4.bin",
        "chunk-1-5.bin",
        "chunk-1-6.bin",
        "chunk-1-7.bin",
        "index.json",
    ]

    chunks = fast_dev_run_enabled_chunks if fast_dev_run == 10 else fast_dev_run_disabled_chunks

    assert sorted(os.listdir(os.path.join(cache_dir, "dummy_dataset"))) == chunks

    files = []
    for _, _, filenames in os.walk(os.path.join(cache_dir, "data")):
        files.extend(filenames)

    expected = (0 if delete_cached_files else 20) if fast_dev_run == 10 else (0 if delete_cached_files else 30)
    assert len(files) == expected


class TestDataProcessor(DataProcessor):
    def _broadcast_object(self, obj: Any) -> Any:
        return obj


@pytest.mark.parametrize("delete_cached_files", [False])
@pytest.mark.parametrize("fast_dev_run", [False])
@pytest.mark.skipif(condition=not _PIL_AVAILABLE or sys.platform == "win32", reason="Requires: ['pil']")
def test_data_processsor_distributed(fast_dev_run, delete_cached_files, tmpdir, monkeypatch):
    """This test ensures the data optimizer works in a fully distributed settings."""

    from PIL import Image

    imgs = []
    for i in range(30):
        np_data = np.random.randint(255, size=(28, 28), dtype=np.uint32)
        img = Image.fromarray(np_data).convert("L")
        imgs.append(img)
        img.save(os.path.join(tmpdir, f"{i}.JPEG"))

    home_dir = os.path.join(tmpdir, "home")
    monkeypatch.setenv("DATA_OPTIMIZER_HOME_FOLDER", home_dir)

    remote_output_dir = os.path.join(tmpdir, "dst")
    os.makedirs(remote_output_dir, exist_ok=True)

    cache_dir = os.path.join(tmpdir, "cache_1")
    monkeypatch.setenv("DATA_OPTIMIZER_CACHE_FOLDER", cache_dir)
    monkeypatch.setenv("DATA_OPTIMIZER_NUM_NODES", "2")
    monkeypatch.setenv("DATA_OPTIMIZER_NODE_RANK", "0")
    data_processor = TestDataProcessor(
        name="dummy_dataset",
        input_dir=tmpdir,
        num_workers=2,
        remote_input_dir=tmpdir,
        delete_cached_files=delete_cached_files,
        fast_dev_run=fast_dev_run,
        remote_output_dir=remote_output_dir,
    )
    data_processor.run(CustomDataChunkRecipe(chunk_size=2))

    assert sorted(os.listdir(cache_dir)) == ["data", "dummy_dataset"]

    fast_dev_run_disabled_chunks_0 = [
        "0-index.json",
        "chunk-0-0.bin",
        "chunk-0-1.bin",
        "chunk-0-2.bin",
        "chunk-0-3.bin",
        "chunk-1-0.bin",
        "chunk-1-1.bin",
        "chunk-1-2.bin",
        "chunk-1-3.bin",
    ]

    assert sorted(os.listdir(os.path.join(cache_dir, "dummy_dataset"))) == fast_dev_run_disabled_chunks_0

    cache_dir = os.path.join(tmpdir, "cache_2")
    monkeypatch.setenv("DATA_OPTIMIZER_CACHE_FOLDER", cache_dir)
    monkeypatch.setenv("DATA_OPTIMIZER_NUM_NODES", "2")
    monkeypatch.setenv("DATA_OPTIMIZER_NODE_RANK", "1")
    data_processor = TestDataProcessor(
        name="dummy_dataset",
        input_dir=tmpdir,
        num_workers=2,
        num_downloaders=1,
        remote_input_dir=tmpdir,
        delete_cached_files=delete_cached_files,
        fast_dev_run=fast_dev_run,
        remote_output_dir=remote_output_dir,
    )
    data_processor.run(CustomDataChunkRecipe(chunk_size=2))

    assert sorted(os.listdir(cache_dir)) == ["data", "dummy_dataset"]

    fast_dev_run_disabled_chunks_1 = [
        "chunk-2-0.bin",
        "chunk-2-1.bin",
        "chunk-2-2.bin",
        "chunk-2-3.bin",
        "chunk-3-0.bin",
        "chunk-3-1.bin",
        "chunk-3-2.bin",
        "chunk-3-3.bin",
        "index.json",
    ]
    assert sorted(os.listdir(os.path.join(cache_dir, "dummy_dataset"))) == fast_dev_run_disabled_chunks_1

    expected = sorted(fast_dev_run_disabled_chunks_0 + fast_dev_run_disabled_chunks_1 + ["1-index.json"])
    assert sorted(os.listdir(remote_output_dir)) == expected


class TextTokenizeRecipe(DataChunkRecipe):
    def prepare_structure(self, input_dir: str) -> List[Any]:
        return [os.path.join(input_dir, "dummy2")]

    def prepare_item(self, filepath):
        for _ in range(100):
            yield torch.randint(0, 1000, (np.random.randint(0, 1000),)).to(torch.int)


@pytest.mark.skipif(condition=sys.platform == "win32", reason="Not supported on windows")
def test_data_processsor_nlp(tmpdir, monkeypatch):
    seed_everything(42)

    monkeypatch.setenv("DATA_OPTIMIZER_CACHE_FOLDER", str(tmpdir))

    with open(os.path.join(tmpdir, "dummy.txt"), "w") as f:
        f.write("Hello World !")

    data_processor = DataProcessor(name="dummy2", input_dir=tmpdir, num_workers=1, num_downloaders=1)
    data_processor.run(TextTokenizeRecipe(chunk_size=1024 * 11))


class ImageResizeRecipe(DataTransformRecipe):
    def prepare_structure(self, input_dir: str):
        filepaths = [os.path.join(input_dir, filename) for filename in os.listdir(input_dir)]
        return [filepath for filepath in filepaths if os.path.isfile(filepath)]

    def prepare_item(self, output_dir: str, filepath: Any) -> None:
        from PIL import Image

        img = Image.open(filepath)
        img = img.resize((12, 12))
        assert os.path.exists(output_dir)
        img.save(os.path.join(output_dir, os.path.basename(filepath)))


@pytest.mark.skipif(condition=not _PIL_AVAILABLE or sys.platform == "win32", reason="Requires: ['pil']")
def test_data_process_transform(monkeypatch, tmpdir):
    from PIL import Image

    imgs = []
    for i in range(5):
        np_data = np.random.randint(255, size=(28, 28), dtype=np.uint32)
        img = Image.fromarray(np_data).convert("L")
        imgs.append(img)
        img.save(os.path.join(tmpdir, f"{i}.JPEG"))

    home_dir = os.path.join(tmpdir, "home")
    cache_dir = os.path.join(tmpdir, "cache")
    remote_output_dir = os.path.join(tmpdir, "target_dir")
    os.makedirs(remote_output_dir, exist_ok=True)
    monkeypatch.setenv("DATA_OPTIMIZER_HOME_FOLDER", home_dir)
    monkeypatch.setenv("DATA_OPTIMIZER_CACHE_FOLDER", cache_dir)
    data_processor = DataProcessor(
        name="dummy_dataset",
        input_dir=tmpdir,
        num_workers=1,
        remote_input_dir=tmpdir,
        remote_output_dir=remote_output_dir,
        fast_dev_run=False,
    )
    data_processor.run(ImageResizeRecipe())

    assert sorted(os.listdir(remote_output_dir)) == ["0.JPEG", "1.JPEG", "2.JPEG", "3.JPEG", "4.JPEG"]

    from PIL import Image

    img = Image.open(os.path.join(remote_output_dir, "0.JPEG"))
    assert img.size == (12, 12)


def map_fn(output_dir, filepath):
    from PIL import Image

    img = Image.open(filepath)
    img = img.resize((12, 12))
    assert os.path.exists(output_dir)
    img.save(os.path.join(output_dir, os.path.basename(filepath)))


@pytest.mark.skipif(condition=not _PIL_AVAILABLE or sys.platform == "win32", reason="Requires: ['pil']")
def test_data_processing_map(monkeypatch, tmpdir):
    from PIL import Image

    input_dir = os.path.join(tmpdir, "input_dir")
    os.makedirs(input_dir, exist_ok=True)
    imgs = []
    for i in range(5):
        np_data = np.random.randint(255, size=(28, 28), dtype=np.uint32)
        img = Image.fromarray(np_data).convert("L")
        imgs.append(img)
        img.save(os.path.join(input_dir, f"{i}.JPEG"))

    home_dir = os.path.join(tmpdir, "home")
    cache_dir = os.path.join(tmpdir, "cache")
    output_dir = os.path.join(tmpdir, "target_dir")
    os.makedirs(output_dir, exist_ok=True)
    monkeypatch.setenv("DATA_OPTIMIZER_HOME_FOLDER", home_dir)
    monkeypatch.setenv("DATA_OPTIMIZER_CACHE_FOLDER", cache_dir)

    resolver = mock.MagicMock()
    resolver.return_value = lambda x: x
    monkeypatch.setattr(functions, "_LightningSrcResolver", resolver)
    monkeypatch.setattr(data_processor_module, "_LightningSrcResolver", resolver)
    monkeypatch.setattr(data_processor_module, "_LightningTargetResolver", resolver)

    inputs = [os.path.join(input_dir, filename) for filename in os.listdir(input_dir)]
    inputs = [filepath for filepath in inputs if os.path.isfile(filepath)]

    map(map_fn, inputs, num_workers=1, output_dir=output_dir, input_dir=input_dir)

    assert sorted(os.listdir(output_dir)) == ["0.JPEG", "1.JPEG", "2.JPEG", "3.JPEG", "4.JPEG"]

    from PIL import Image

    img = Image.open(os.path.join(output_dir, "0.JPEG"))
    assert img.size == (12, 12)


def optimize_fn(filepath):
    print(filepath)
    from PIL import Image

    return [Image.open(filepath), os.path.basename(filepath)]


@pytest.mark.skipif(condition=not _PIL_AVAILABLE or sys.platform == "win32", reason="Requires: ['pil']")
def test_data_processing_optimize(monkeypatch, tmpdir):
    from PIL import Image

    input_dir = os.path.join(tmpdir, "input_dir")
    os.makedirs(input_dir, exist_ok=True)
    imgs = []
    for i in range(5):
        np_data = np.random.randint(255, size=(28, 28), dtype=np.uint32)
        img = Image.fromarray(np_data).convert("L")
        imgs.append(img)
        img.save(os.path.join(input_dir, f"{i}.JPEG"))

    home_dir = os.path.join(tmpdir, "home")
    cache_dir = os.path.join(tmpdir, "cache")
    output_dir = os.path.join(tmpdir, "target_dir")
    os.makedirs(output_dir, exist_ok=True)
    monkeypatch.setenv("DATA_OPTIMIZER_HOME_FOLDER", home_dir)
    monkeypatch.setenv("DATA_OPTIMIZER_CACHE_FOLDER", cache_dir)

    inputs = [os.path.join(input_dir, filename) for filename in os.listdir(input_dir)]
    inputs = [filepath for filepath in inputs if os.path.isfile(filepath)]

    resolver = mock.MagicMock()
    resolver.return_value = lambda x: x
    monkeypatch.setattr(functions, "_LightningSrcResolver", resolver)
    monkeypatch.setattr(data_processor_module, "_LightningSrcResolver", resolver)
    monkeypatch.setattr(data_processor_module, "_LightningTargetResolver", resolver)

    optimize(optimize_fn, inputs, num_workers=1, output_dir=output_dir, chunk_size=2, input_dir=input_dir)

    assert sorted(os.listdir(output_dir)) == ["chunk-0-0.bin", "chunk-0-1.bin", "chunk-0-2.bin", "index.json"]

    cache = Cache(output_dir, chunk_size=1)
    assert len(cache) == 5
