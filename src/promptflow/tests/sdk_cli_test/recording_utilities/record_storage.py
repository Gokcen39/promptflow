# ---------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# ---------------------------------------------------------
import hashlib
import json
import os
import shelve
from pathlib import Path
from typing import Dict, Iterator, Union

from filelock import FileLock

from promptflow._core.generator_proxy import GeneratorProxy
from promptflow.exceptions import PromptflowException

from .constants import ENVIRON_TEST_MODE, RecordMode


def get_test_mode_from_environ() -> str:
    return os.getenv(ENVIRON_TEST_MODE, RecordMode.LIVE)


def is_record() -> bool:
    return get_test_mode_from_environ() == RecordMode.RECORD


def is_replay() -> bool:
    return get_test_mode_from_environ() == RecordMode.REPLAY


def is_live() -> bool:
    return get_test_mode_from_environ() == RecordMode.LIVE


class RecordItemMissingException(PromptflowException):
    """Exception raised when record item missing."""

    pass


class RecordFileMissingException(PromptflowException):
    """Exception raised when record file missing or invalid."""

    pass


class RecordFile:
    _standard_record_folder = ".promptflow"
    _standard_record_name = "node_cache.shelve"

    def __init__(self, record_file_input):
        self.real_file: Path = None
        self.real_file_parent: Path = None
        self.record_file_str: str = None

    def get(self) -> Path:
        return self.real_file

    def set_and_load_file(self, record_file_input, cached_items):
        """
        Will load record_file if exist.
        """
        record_file = Path(record_file_input).resolve()
        if record_file == self.real_file:
            return

        self.real_file = record_file
        if not self.real_file.parts[-1].endswith(RecordFile._standard_record_name):
            self.real_file_parent = self.real_file / RecordFile._standard_record_folder
            self.real_file = self.real_file_parent / RecordFile._standard_record_name
        else:
            self.real_file_parent = self.real_file.parent
        self.record_file_str = str(self.real_file)

        # cache folder we could create if not exist.
        if not self.real_file_parent.exists():
            self.real_file_parent.mkdir(parents=True, exist_ok=True)

        self.load_file(cached_items)

    def write_file(self, file_records, hashkey) -> None:
        if file_records is not None:
            file_content_line = file_records.get(hashkey, None)
            if file_content_line is not None:
                lock = FileLock(self.real_file.parent / "record_file.lock")
                with lock:
                    saved_dict = shelve.open(self.record_file_str, "c", writeback=False)
                    saved_dict[hashkey] = file_content_line
                    saved_dict.close()
            else:
                raise RecordItemMissingException(f"Record item not found in cache with hashkey {hashkey}.")
        else:
            raise RecordFileMissingException(
                f"This exception should not happen here, but record file is not found {self.record_file_str}."
            )

    def recording_file_exists(self) -> bool:
        files = os.listdir(self.real_file_parent)
        for file in files:
            if file.startswith(self.real_file.parts[-1]):
                return True
        return False

    def load_file(self, cached_items) -> bool:
        # if file not exist, just exit and create an empty cache slot.
        if not self.recording_file_exists():
            cached_items[self.record_file_str] = {
                self.record_file_str: {},
            }
            return False

        # Load file directly.
        lock = FileLock(self.real_file.parent / "record_file.lock")
        with lock:
            saved_dict = shelve.open(self.record_file_str, "r", writeback=False)
            cached_items[self.record_file_str] = {}
            for key, value in saved_dict.items():
                cached_items[self.record_file_str][key] = value
            saved_dict.close()

        return True

    def delete_lock_file(self):
        lock_file = self.real_file_parent / "record_file.lock"
        if lock_file.exists():
            os.remove(lock_file)


class RecordCache:
    """
    RecordCache is used to store the record of node run.
    File often stored in .promptflow/node_cache.shelve
    Currently only text input/output could be recorded.
    Example of cached items:
    {
        "/record/file/resolved": {  <-- file_records_pointer
            "hash_value": { <-- line_record_pointer   hash_value is sha1 of dict, accelerate the search
                "input": {
                    "key1": "value1", # Converted to string, type info dropped
                },
                "output": "output_convert_to_string",
                "output_type": "output_type" # Currently support only simple strings.
            }
        }
    }
    """

    def __init__(self):
        self.cached_items: Dict[str, Dict[str, Dict[str, object]]] = {}
        self.record_file = None
        self.file_records_pointer = {}

    def get_cache(self, record_file: Union[str, Path]) -> None:
        if self.record_file is None:
            self.record_file = RecordFile(record_file)
            self.record_file.set_and_load_file(record_file, self.cached_items)
        else:
            if self.record_file.get() == Path(record_file):
                return
            else:
                self.record_file.set_and_load_file(Path(record_file))

                self.cached_items = self.record_file.load_file(self.cached_items)
        try:
            self.file_records_pointer = self.cached_items[self.record_file.record_file_str]
        except KeyError:
            self.cached_items[self.record_file.record_file_str] = {}
            self.file_records_pointer = self.cached_items[self.record_file.record_file_str]
            self.write_back(None)

    def _recursive_create_hashable_args(self, item):
        if isinstance(item, tuple):
            return [self._recursive_create_hashable_args(i) for i in item]
        if isinstance(item, list):
            return [self._recursive_create_hashable_args(i) for i in item]
        if isinstance(item, dict):
            return {k: self._recursive_create_hashable_args(v) for k, v in item.items() if k != "extra_headers"}
        elif "module: promptflow.connections" in str(item) or "object at" in str(item):
            return []
        else:
            return item

    @staticmethod
    def _parse_output_generator(output):
        """
        Special handling for generator type. Since pickle will not work for generator.
        Returns the real list for reocrding, and create a generator for original output.
        Parse output has a simplified hypothesis: output is simple dict, list or generator,
        because a full schema of output is too heavy to handle.
        Example: {"answer": <generator>, "a": "b"}, <generator>
        """
        output_type = ""
        output_value = None
        output_generator = None
        if isinstance(output, dict):
            output_value = {}
            output_generator = {}
            for item in output.items():
                k, v = item
                if type(v).__name__ == "generator":
                    vlist = list(v)

                    def vgenerator():
                        for vitem in vlist:
                            yield vitem

                    output_value[k] = vlist
                    output_generator[k] = vgenerator()
                    output_type = "dict[generator]"
                else:
                    output_value[k] = v
        elif isinstance(output, Iterator):
            output = GeneratorProxy(output)
            output_value = list(output)

            def generator():
                for item in output_value:
                    yield item

            output_generator = generator()
            output_type = "generator"
        else:
            output_value = output
            output_generator = None
            output_type = type(output).__name__
        return output_value, output_generator, output_type

    @staticmethod
    def _create_output_generator(output, output_type):
        """
        Special handling for generator type.
        Returns a generator for original output.
        Create output has a simplified hypothesis:
        All list with output type generator is treated as generator.
        """
        output_generator = None
        if output_type == "dict[generator]":
            output_generator = {}
            for k, v in output.items():
                if type(v).__name__ == "list":

                    def vgenerator():
                        for item in v:
                            yield item

                    output_generator[k] = vgenerator()
                else:
                    output_generator[k] = v
        elif output_type == "generator":

            def generator():
                for item in output:
                    yield item

            output_generator = generator()
        return output_generator

    def get_record(self, input_dict: Dict) -> object:
        """
        Get record from local storage.

        :param input_dict: input dict of critical AOAI inputs
        :type input_dict: Dict
        :raises RecordFileMissingException: Record file not exist
        :raises RecordItemMissingException: Record item not exist in record file
        :return: original output of node run
        :rtype: object
        """
        input_dict = self._recursive_create_hashable_args(input_dict)
        hash_value: str = hashlib.sha1(str(sorted(input_dict.items())).encode("utf-8")).hexdigest()

        try:
            line_record_pointer = self.file_records_pointer[hash_value]
        except KeyError:
            raise RecordItemMissingException(
                f"Record item not found in file {self.record_file.record_file_str}.\n"
                f"values: {json.dumps(input_dict)}\n"
            )

        # not all items are reserved in the output dict.
        output = line_record_pointer["output"]
        output_type = line_record_pointer["output_type"]
        if "generator" in output_type:
            return RecordCache._create_output_generator(output, output_type)
        else:
            return output

    def write_back(self, hash_value):
        self.cached_items[self.record_file.record_file_str] = self.file_records_pointer
        if hash_value is not None:
            self.record_file.write_file(self.file_records_pointer, hash_value)

    def set_record(self, input_dict: Dict, output):
        """
        Set record to local storage, always override the old record.

        :param input_dict: input dict of critical AOAI inputs
        :type input_dict: OrderedDict
        :param output: original output of node run
        :type output: object
        """
        # filter args, object at will not hash
        input_dict = self._recursive_create_hashable_args(input_dict)
        hash_value: str = hashlib.sha1(str(sorted(input_dict.items())).encode("utf-8")).hexdigest()
        output_value, output_generator, output_type = RecordCache._parse_output_generator(output)

        try:
            line_record_pointer = self.file_records_pointer[hash_value]
        except KeyError:
            self.file_records_pointer[hash_value] = {
                "input": input_dict,
                "output": output_value,
                "output_type": output_type,
            }
            line_record_pointer = self.file_records_pointer[hash_value]
            self.write_back(hash_value)

        if line_record_pointer["output"] == output_value and line_record_pointer["output_type"] == output_type:
            # no writeback
            if "generator" in output_type:
                return output_generator
            else:
                return output_value
        else:
            self.file_records_pointer[hash_value] = {
                "input": input_dict,
                "output": output_value,
                "output_type": output_type,
            }

            self.write_back(hash_value)

            if "generator" in output_type:
                return output_generator
            else:
                return output_value


class RecordStorage:
    _instance = None

    def __init__(self):
        self.record_cache = RecordCache()

    def set_file(self, record_file: Union[str, Path]) -> None:
        """
        Will load record_file if exist.
        """
        self.record_cache.get_cache(record_file)

    def get_record(self, input_dict: Dict) -> object:
        """
        Get record from local storage.

        :param input_dict: input dict of critical AOAI inputs
        :type input_dict: Dict
        :raises RecordFileMissingException: Record file not exist
        :raises RecordItemMissingException: Record item not exist in record file
        :return: original output of node run
        :rtype: object
        """
        return self.record_cache.get_record(input_dict)

    def set_record(self, input_dict: Dict, output):
        """
        Set record to local storage, always override the old record.

        :param input_dict: input dict of critical AOAI inputs
        :type input_dict: OrderedDict
        :param output: original output of node run
        :type output: object
        """
        return self.record_cache.set_record(input_dict, output)

    def delete_lock_file(self):
        if self.record_cache.record_file:
            self.record_cache.record_file.delete_lock_file()

    @classmethod
    def get_instance(cls, record_file=None) -> "RecordStorage":
        """
        Use this to get instance to avoid multiple copies of same record storage.

        :param record_file: initiate at first entrance, defaults to None in the first call will raise exception.
        :type record_file: str or Path, optional
        :return: instance of RecordStorage
        :rtype: RecordStorage
        """
        # if not in recording mode, return None
        if not (is_record() or is_replay() or is_live()):
            return None
        # Create instance if not exist
        if is_record() or is_replay():
            if cls._instance is None:
                if record_file is None:
                    raise RecordFileMissingException("record_file is value None")
                cls._instance = RecordStorage()
            if record_file is not None:
                cls._instance.set_file(record_file)
        else:
            if cls._instance is None:
                cls._instance = RecordStorage()  # live mode return an empty record storage
        return cls._instance


class Counter:
    _instance = None

    def __init__(self):
        self.file = None

    def is_non_zero_file(self, fpath):
        return os.path.isfile(fpath) and os.path.getsize(fpath) > 0

    def set_file_record_count(self, file, obj):
        """
        Just count how many tokens are calculated. Different from
        openai_metric_calculator, this is directly returned from AOAI.
        """
        output_value, output_generator, output_type = RecordCache._parse_output_generator(obj)
        if "generator" in output_type:
            count = len(output_value)
        elif hasattr(output_value, "usage") and output_value.usage and output_value.usage.total_tokens:
            count = output_value.usage.total_tokens
        else:
            # This is error. Suppress it.
            count = 0

        self.file = file
        with FileLock(str(file) + ".lock"):
            is_non_zero_file = self.is_non_zero_file(file)
            if is_non_zero_file:
                with open(file, "r", encoding="utf-8") as f:
                    number = json.load(f)
                    number["count"] += count
            else:
                number = {"count": count}
            with open(file, "w", encoding="utf-8") as f:
                number_str = json.dumps(number, ensure_ascii=False)
                f.write(number_str)

        if "generator" in output_type:
            return output_generator
        else:
            return output_value

    @classmethod
    def get_instance(cls) -> "Counter":
        if cls._instance is None:
            cls._instance = Counter()
        return cls._instance
