from hub.core.version_control.commit_chunk_set import CommitChunkSet
from hub.core.version_control.commit_diff import CommitDiff
from hub.core.chunk.base_chunk import InputSample
import numpy as np
from typing import Dict, List, Sequence, Union, Optional, Tuple, Any
from functools import reduce
from hub.core.index import Index
from hub.core.meta.tensor_meta import TensorMeta
from hub.core.storage import StorageProvider, LRUCache
from hub.core.chunk_engine import ChunkEngine
from hub.api.info import load_info
from hub.util.keys import (
    get_chunk_id_encoder_key,
    get_chunk_key,
    get_tensor_commit_chunk_set_key,
    get_tensor_commit_diff_key,
    get_tensor_meta_key,
    tensor_exists,
    get_tensor_info_key,
)
from hub.util.keys import get_tensor_meta_key, tensor_exists, get_tensor_info_key
from hub.util.shape_interval import ShapeInterval
from hub.util.exceptions import (
    TensorDoesNotExistError,
    InvalidKeyTypeError,
    TensorAlreadyExistsError,
)
from hub.constants import FIRST_COMMIT_ID
from hub.util.version_control import auto_checkout


def create_tensor(
    key: str,
    storage: StorageProvider,
    htype: str,
    sample_compression: str,
    chunk_compression: str,
    version_state: Dict[str, Any],
    **kwargs,
):
    """If a tensor does not exist, create a new one with the provided meta.

    Args:
        key (str): Key for where the chunks, index_meta, and tensor_meta will be located in `storage` relative to it's root.
        storage (StorageProvider): StorageProvider that all tensor data is written to.
        htype (str): Htype is how the default tensor metadata is defined.
        sample_compression (str): All samples will be compressed in the provided format. If `None`, samples are uncompressed.
        chunk_compression (str): All chunks will be compressed in the provided format. If `None`, chunks are uncompressed.
        version_state (Dict[str, Any]): The version state of the dataset, includes commit_id, commit_node, branch, branch_commit_map and commit_node_map.
        **kwargs: `htype` defaults can be overridden by passing any of the compatible parameters.
            To see all `htype`s and their correspondent arguments, check out `hub/htypes.py`.

    Raises:
        TensorAlreadyExistsError: If a tensor defined with `key` already exists.
    """

    commit_id = version_state["commit_id"]
    if tensor_exists(key, storage, commit_id):
        raise TensorAlreadyExistsError(key)

    meta_key = get_tensor_meta_key(key, commit_id)
    meta = TensorMeta(
        htype=htype,
        sample_compression=sample_compression,
        chunk_compression=chunk_compression,
        **kwargs,
    )
    storage[meta_key] = meta  # type: ignore

    if commit_id != FIRST_COMMIT_ID:
        cset_key = get_tensor_commit_chunk_set_key(key, commit_id)
        cset = CommitChunkSet()
        storage[cset_key] = cset  # type: ignore

    diff_key = get_tensor_commit_diff_key(key, commit_id)
    diff = CommitDiff(created=True)
    storage[diff_key] = diff  # type: ignore


def delete_tensor(key: str, dataset):
    """Delete tensor from storage.

    Args:
        key (str): Key for where the chunks, index_meta, and tensor_meta will be located in `storage` relative to it's root.
        dataset (Dataset): Dataset that the tensor is located in.

    Raises:
        TensorDoesNotExistError: If no tensor with `key` exists and a `tensor_meta` was not provided.
    """
    storage = dataset.storage
    version_state = dataset.version_state

    if not tensor_exists(key, storage, version_state["commit_id"]):
        raise TensorDoesNotExistError(key)

    tensor = Tensor(key, dataset)
    chunk_engine = tensor.chunk_engine
    enc = chunk_engine.chunk_id_encoder
    n_chunks = chunk_engine.num_chunks
    chunk_names = [enc.get_name_for_chunk(i) for i in range(n_chunks)]
    chunk_keys = [
        get_chunk_key(key, chunk_name, version_state["commit_id"])
        for chunk_name in chunk_names
    ]
    for chunk_key in chunk_keys:
        try:
            del storage[chunk_key]
        except KeyError:
            pass

    commit_id = version_state["commit_id"]
    meta_key = get_tensor_meta_key(key, commit_id)
    try:
        del storage[meta_key]
    except KeyError:
        pass

    info_key = get_tensor_info_key(key, commit_id)
    try:
        del storage[info_key]
    except KeyError:
        pass

    diff_key = get_tensor_commit_diff_key(key, commit_id)
    try:
        del storage[diff_key]
    except KeyError:
        pass

    chunk_id_encoder_key = get_chunk_id_encoder_key(key, commit_id)
    try:
        del storage[chunk_id_encoder_key]
    except KeyError:
        pass


def _inplace_op(f):
    op = f.__name__

    def inner(tensor, other):
        tensor._write_initialization()
        tensor.chunk_engine.update(tensor.index, other, op)
        if not tensor.index.is_trivial():
            tensor._skip_next_setitem = True
        return tensor

    return inner


class Tensor:
    def __init__(
        self,
        key: str,
        dataset,
        index: Optional[Index] = None,
        is_iteration: bool = False,
        chunk_engine: Optional[ChunkEngine] = None,
    ):
        """Initializes a new tensor.

        Note:
            This operation does not create a new tensor in the storage provider,
            and should normally only be performed by Hub internals.

        Args:
            key (str): The internal identifier for this tensor.
            dataset (Dataset): The dataset that this tensor is located in.
            index: The Index object restricting the view of this tensor.
                Can be an int, slice, or (used internally) an Index object.
            is_iteration (bool): If this tensor is being used as an iterator.
            chunk_engine (ChunkEngine, optional): The underlying chunk_engine for the tensor

        Raises:
            TensorDoesNotExistError: If no tensor with `key` exists and a `tensor_meta` was not provided.
        """
        self.key = key
        self.dataset = dataset
        self.storage = dataset.storage
        self.index = index or Index()
        self.version_state = dataset.version_state
        self.is_iteration = is_iteration

        if not self.is_iteration and not tensor_exists(
            self.key, self.storage, self.version_state["commit_id"]
        ):
            raise TensorDoesNotExistError(self.key)

        self.chunk_engine = chunk_engine or ChunkEngine(
            self.key, self.storage, self.version_state
        )

        if not self.is_iteration:
            self.index.validate(self.num_samples)
        self._info = None

        # An optimization to skip multiple .numpy() calls when performing inplace ops on slices:
        self._skip_next_setitem = False

    def _write_initialization(self):
        self.storage.check_readonly()
        # if not the head node, checkout to an auto branch that is newly created
        auto_checkout(self.dataset)

    def extend(self, samples: Union[np.ndarray, Sequence[InputSample], "Tensor"]):

        """Extends the end of the tensor by appending multiple elements from a sequence. Accepts a sequence, a single batched numpy array,
        or a sequence of `hub.read` outputs, which can be used to load files. See examples down below.

        Example:
            numpy input:
                >>> len(tensor)
                0
                >>> tensor.extend(np.zeros((100, 28, 28, 1)))
                >>> len(tensor)
                100

            file input:
                >>> len(tensor)
                0
                >>> tensor.extend([
                        hub.read("path/to/image1"),
                        hub.read("path/to/image2"),
                    ])
                >>> len(tensor)
                2


        Args:
            samples (np.ndarray, Sequence, Sequence[Sample]): The data to add to the tensor.
                The length should be equal to the number of samples to add.

        Raises:
            TensorDtypeMismatchError: TensorDtypeMismatchError: Dtype for array must be equal to or castable to this tensor's dtype
        """
        self._write_initialization()
        self.chunk_engine.extend(samples)

    @property
    def info(self):
        """Returns the information about the tensor.

        Returns:
            TensorInfo: Information about the tensor.
        """

        if self._info is None:
            self._info = load_info(
                get_tensor_info_key(self.key, self.version_state["commit_id"]),
                self.storage,
                self.dataset,
            )
        return self._info

    def append(self, sample: InputSample):
        """Appends a single sample to the end of the tensor. Can be an array, scalar value, or the return value from `hub.read`,
        which can be used to load files. See examples down below.

        Examples:
            numpy input:
                >>> len(tensor)
                0
                >>> tensor.append(np.zeros((28, 28, 1)))
                >>> len(tensor)
                1

            file input:
                >>> len(tensor)
                0
                >>> tensor.append(hub.read("path/to/file"))
                >>> len(tensor)
                1

        Args:
            sample (InputSample): The data to append to the tensor. `Sample` is generated by `hub.read`. See the above examples.
        """
        self.extend([sample])

    @property
    def meta(self):
        return self.chunk_engine.tensor_meta

    @property
    def shape(self) -> Tuple[Optional[int], ...]:
        """Get the shape of this tensor. Length is included.

        Note:
            If you don't want `None` in the output shape or want the lower/upper bound shapes,
            use `tensor.shape_interval` instead.

        Example:
            >>> tensor.append(np.zeros((10, 10)))
            >>> tensor.append(np.zeros((10, 15)))
            >>> tensor.shape
            (2, 10, None)

        Returns:
            tuple: Tuple where each value is either `None` (if that axis is dynamic) or
                an `int` (if that axis is fixed).
        """
        shape = self.shape_interval.astuple()
        if None in shape:
            if not self.index.values[0].subscriptable():
                shape = self.chunk_engine.read_shape_for_sample(self.index.values[0].value)  # type: ignore
        elif not self.index.values[0].subscriptable():
            shape = shape[1:]
        shape = list(shape)  # type: ignore
        squeeze_dims = set()
        for i, idx in enumerate(self.index.values[1:]):
            shape[i] = len(list(idx.indices(shape[i])))  # type: ignore
            if not idx.subscriptable():
                squeeze_dims.add(i)
        return tuple(shape[i] for i in range(len(shape)) if i not in squeeze_dims)

    @property
    def ndim(self) -> int:
        return len(self.shape)

    @property
    def dtype(self) -> Optional[np.dtype]:
        if self.htype in ("json", "list"):
            return np.dtype(str)
        if self.meta.dtype:
            return np.dtype(self.meta.dtype)
        return None

    @property
    def htype(self):
        return self.meta.htype

    @property
    def shape_interval(self) -> ShapeInterval:
        """Returns a `ShapeInterval` object that describes this tensor's shape more accurately. Length is included.

        Note:
            If you are expecting a `tuple`, use `tensor.shape` instead.

        Example:
            >>> tensor.append(np.zeros((10, 10)))
            >>> tensor.append(np.zeros((10, 15)))
            >>> tensor.shape_interval
            ShapeInterval(lower=(2, 10, 10), upper=(2, 10, 15))
            >>> str(tensor.shape_interval)
            (2, 10, 10:15)

        Returns:
            ShapeInterval: Object containing `lower` and `upper` properties.
        """

        length = [len(self)]

        min_shape = length + list(self.meta.min_shape)
        max_shape = length + list(self.meta.max_shape)

        return ShapeInterval(min_shape, max_shape)

    @property
    def is_dynamic(self) -> bool:
        """Will return True if samples in this tensor have shapes that are unequal."""
        return self.shape_interval.is_dynamic

    @property
    def num_samples(self) -> int:
        """Returns the length of the primary axis of the tensor.
        Ignores any applied indexing and returns the total length.
        """
        return self.chunk_engine.tensor_meta.length

    def __len__(self):
        """Returns the length of the primary axis of the tensor.
        Accounts for indexing into the tensor object.

        Examples:
            >>> len(tensor)
            0
            >>> tensor.extend(np.zeros((100, 10, 10)))
            >>> len(tensor)
            100
            >>> len(tensor[5:10])
            5

        Returns:
            int: The current length of this tensor.
        """

        # catch corrupted datasets / user tampering ASAP
        self.chunk_engine.validate_num_samples_is_synchronized()

        return self.index.length(self.meta.length)

    def __getitem__(
        self,
        item: Union[int, slice, List[int], Tuple[Union[int, slice, Tuple[int]]], Index],
        is_iteration: bool = False,
    ):
        if not isinstance(item, (int, slice, list, tuple, Index)):
            raise InvalidKeyTypeError(item)
        return Tensor(
            self.key,
            self.dataset,
            index=self.index[item],
            is_iteration=is_iteration,
            chunk_engine=self.chunk_engine,
        )

    def _get_bigger_dtype(self, d1, d2):
        if np.can_cast(d1, d2):
            if np.can_cast(d2, d1):
                return d1
            else:
                return d2
        else:
            if np.can_cast(d2, d1):
                return d2
            else:
                return np.object

    def _infer_np_dtype(self, val: Any) -> np.dtype:
        # TODO refac
        if hasattr(val, "dtype"):
            return val.dtype
        elif isinstance(val, int):
            return np.array(0).dtype
        elif isinstance(val, float):
            return np.array(0.0).dtype
        elif isinstance(val, str):
            return np.array("").dtype
        elif isinstance(val, bool):
            return np.dtype(bool)
        elif isinstance(val, Sequence):
            return reduce(self._get_bigger_dtype, map(self._infer_np_dtype, val))
        else:
            raise TypeError(f"Cannot infer numpy dtype for {val}")

    def __setitem__(self, item: Union[int, slice], value: Any):
        """Update samples with new values.

        Example:
            >>> tensor.append(np.zeros((10, 10)))
            >>> tensor.shape
            (1, 10, 10)
            >>> tensor[0] = np.zeros((3, 3))
            >>> tensor.shape
            (1, 3, 3)
        """
        self._write_initialization()
        if isinstance(value, Tensor):
            if value._skip_next_setitem:
                value._skip_next_setitem = False
                return
            value = value.numpy(aslist=True)
        item_index = Index(item)
        self.chunk_engine.update(self.index[item_index], value)

    def __iter__(self):
        for i in range(len(self)):
            yield self.__getitem__(i, is_iteration=True)

    def numpy(self, aslist=False) -> Union[np.ndarray, List[np.ndarray]]:
        """Computes the contents of the tensor in numpy format.

        Args:
            aslist (bool): If True, a list of np.ndarrays will be returned. Helpful for dynamic tensors.
                If False, a single np.ndarray will be returned unless the samples are dynamically shaped, in which case
                an error is raised.

        Raises:
            DynamicTensorNumpyError: If reading a dynamically-shaped array slice without `aslist=True`.

        Returns:
            A numpy array containing the data represented by this tensor.
        """

        return self.chunk_engine.numpy(self.index, aslist=aslist)

    def __str__(self):
        index_str = f", index={self.index}"
        if self.index.is_trivial():
            index_str = ""
        return f"Tensor(key={repr(self.key)}{index_str})"

    __repr__ = __str__

    def __array__(self) -> np.ndarray:
        return self.numpy()  # type: ignore

    @_inplace_op
    def __iadd__(self, other):
        pass

    @_inplace_op
    def __isub__(self, other):
        pass

    @_inplace_op
    def __imul__(self, other):
        pass

    @_inplace_op
    def __itruediv__(self, other):
        pass

    @_inplace_op
    def __ifloordiv__(self, other):
        pass

    @_inplace_op
    def __imod__(self, other):
        pass

    @_inplace_op
    def __ipow__(self, other):
        pass

    @_inplace_op
    def __ilshift__(self, other):
        pass

    @_inplace_op
    def __irshift__(self, other):
        pass

    @_inplace_op
    def __iand__(self, other):
        pass

    @_inplace_op
    def __ixor__(self, other):
        pass

    @_inplace_op
    def __ior__(self, other):
        pass

    def data(self) -> Any:
        htype = self.htype
        if htype in ("json", "text"):

            if self.ndim == 1:
                return self.numpy()[0]
            else:
                return [sample[0] for sample in self.numpy(aslist=True)]
        elif htype == "list":
            if self.ndim == 1:
                return list(self.numpy())
            else:
                return list(map(list, self.numpy(aslist=True)))
        else:
            return self.numpy()

    def tobytes(self) -> bytes:
        """Returns the bytes of the tensor. Only works for a single sample of tensor.
        If the tensor is uncompressed, this returns the bytes of the numpy array.
        If the tensor is sample compressed, this returns the compressed bytes of the sample.
        If the tensor is chunk compressed, this raises an error.

        Returns:
            bytes: The bytes of the tensor.

        Raises:
            ValueError: If the tensor has multiple samples.
        """
        if self.index.values[0].subscriptable() or len(self.index.values) > 1:
            raise ValueError("tobytes() can be used only on exatcly 1 sample.")
        return self.chunk_engine.read_bytes_for_sample(self.index.values[0].value)  # type: ignore

    def _pop(self):
        self.chunk_engine._pop()
