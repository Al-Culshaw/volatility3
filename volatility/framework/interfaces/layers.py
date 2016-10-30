"""
Created on 4 May 2013

@author: mike
"""
import collections
import collections.abc
from abc import ABCMeta, abstractmethod, abstractproperty

from volatility.framework import exceptions, validity
from volatility.framework.interfaces import configuration, context


class ScannerInterface(validity.ValidityRoutines, metaclass = ABCMeta):
    """Class for layer scanners that return locations of particular values from within the data

    These are designed to be given a chunk of data and return a generator which yields
    any found items.  They should NOT perform complex/time-consuming tasks, these should
    be carried out by the consumer of the generator on the items returned.

    They will be provided all *available* data (therefore not necessarily contiguous)
    in ascending offset order, in chunks no larger than chunk_size + overlap where
    overlap is the amount of data read twice once at the end of an earlier chunk and
    once at the start of the next chunk.

    It should be noted that the scanner can maintain state if necessary.
    Scanners should balance the size of chunk based on the amount of time
    scanning the chunk will take (ie, do not set an excessively large chunksize
    and try not to take a significant amount of time in the __call__ method).
    """

    def __init__(self):
        self.chunk_size = 0x1000000  # Default to 16Mb chunks
        self.overlap = 0x1000  # A page of overlap by default
        self._context = None
        self._layer_name = None

    @property
    def context(self):
        return self._context

    @context.setter
    def context(self, ctx):
        """Stores the context locally in case the scanner needs to access the layer"""
        self._context = self._check_type(ctx, context.ContextInterface)

    @property
    def layer_name(self):
        return self._layer_name

    @layer_name.setter
    def layer_name(self, layer_name):
        """Stores the layer_name being scanned locally in case the scanner needs to access the layer"""
        self._layer_name = self._check_type(layer_name, str)

    @abstractmethod
    def __call__(self, data, data_offset):
        """Searches through a chunk of data for a particular value/pattern/etc
           Always returns an iterator of the same type of object (need not be a volatility object)

           data is the chunk of data to search through
           data_offset is the offset within the layer that the data being searched starts at
        """


class DataLayerInterface(configuration.ConfigurableInterface, validity.ValidityRoutines, metaclass = ABCMeta):
    """A Layer that directly holds data (and does not translate it"""

    provides = {"type": "interface"}

    def __init__(self, context, config_path, name):
        super().__init__(context, config_path)
        self._name = self._check_type(name, str)

    @property
    def name(self):
        """Returns the layer name"""
        return self._name

    @abstractproperty
    def maximum_address(self):
        """Returns the maximum valid address of the space"""

    @abstractproperty
    def minimum_address(self):
        """Returns the minimum valid address of the space"""

    @abstractmethod
    def is_valid(self, offset, length = 1):
        """Returns a boolean based on whether the offset is valid or not"""

    @abstractmethod
    def read(self, offset, length, pad = False):
        """Reads an offset for length bytes and returns 'bytes' (not 'str') of length size

           If there is a fault of any kind (such as a page fault), an exception will be thrown
           unless pad is set, in which case the read errors will be replaced by null characters.
        """

    @abstractmethod
    def write(self, offset, data):
        """Writes a chunk of data at offset.

           Any unavailable sections in the underlying bases will cause an exception to be thrown.
           Note: Writes are not atomic, therefore some data can be written, even if an exception is thrown.
        """

    def destroy(self):
        """Allows DataLayers to close any open handles, etc.

           Systems that make use of Data Layers should called destroy when they are done with them.
           This will close all handles, and make the object unreadable
           (exceptions will be thrown using a DataLayer after destruction)"""
        pass

    @classmethod
    def get_requirements(cls):
        """Returns a list of Requirement objects for this type of layer"""
        return []

    # ## General scanning methods

    def _pre_scan(self, context, min_address, max_address, progress_callback, scanner):
        """Prepares the scanner based on standard procedures shared between TranslationLayers and DataLayers"""
        if progress_callback is not None:
            self._check_type(progress_callback, collections.Callable)

        scanner = self._check_type(scanner, ScannerInterface)
        scanner.context = context
        scanner.layer_name = self.name

        if min_address is None:
            min_address = self.minimum_address
        if max_address is None:
            max_address = self.maximum_address

        min_address = min(self.minimum_address, min_address)
        max_address = max(self.maximum_address, max_address)
        total_size = (max_address - min_address)
        return min_address, max_address, scanner, total_size

    def scan(self, context, scanner, progress_callback = None, min_address = None, max_address = None):
        """Scans the layer using scanner, between min_address and max_address calling progress_callback at internvals to report percentage progress"""
        min_address, max_address, scanner, total_size = self._pre_scan(context, min_address, max_address,
                                                                       progress_callback, scanner)

        for offset in range(min_address, max_address, scanner.chunk_size):
            length = min(scanner.chunk_size + scanner.overlap, max_address - offset)
            chunk = self.read(offset, length)
            if progress_callback:
                progress_callback((offset * 100) / total_size)
            for x in scanner(chunk, offset):
                yield x

    def build_configuration(self):
        config = super().build_configuration()

        # Translation Layers are constructable, and therefore require a class configuration variable
        config["class"] = self.__class__.__module__ + "." + self.__class__.__name__
        return config


class TranslationLayerInterface(DataLayerInterface, metaclass = ABCMeta):
    # Unfortunately class attributes can't easily be inheritted from parent classes
    provides = {"type": "interface"}

    @abstractmethod
    def mapping(self, offset, length, ignore_errors = False):
        """Returns a sorted iterable of (offset, mapped_offset, length, layer) mappings

           ignore_errors will provide all available maps with gaps, but their total length may not add up to the requested length
           This allows translation layers to provide maps of contiguous regions in one layer
        """
        return []

    @property
    @abstractmethod
    def dependencies(self):
        """Returns a list of layer names that this layer translates onto"""
        return []

    # ## Read/Write functions for mapped pages

    def read(self, offset, length, pad = False):
        """Reads an offset for length bytes and returns 'bytes' (not 'str') of length size"""
        current_offset = offset
        output = []
        for (offset, mapped_offset, length, layer) in self.mapping(offset, length, ignore_errors = pad):
            if not pad and offset > current_offset:
                raise exceptions.InvalidAddressException(self.name, current_offset,
                                                         "Layer {} cannot map offset: {}".format(self.name,
                                                                                                 current_offset))
            elif offset > current_offset:
                output += [b"\x00" * (current_offset - offset)]
                current_offset = offset
            elif offset < current_offset:
                raise exceptions.LayerException("Mapping returned an overlapping element")
            output += [self._context.memory.read(layer, mapped_offset, length, pad)]
            current_offset += length
        recovered_data = b"".join(output)
        return recovered_data + b"\x00" * (length - len(recovered_data))

    def write(self, offset, value):
        """Writes a value at offset, distributing the writing across any underlying mapping"""
        current_offset = offset
        length = len(value)
        for (offset, mapped_offset, length, layer) in self.mapping(offset, length):
            if offset > current_offset:
                raise exceptions.InvalidAddressException(self.name, current_offset,
                                                         "Layer {} cannot map offset: {}".format(self.name,
                                                                                                 current_offset))
            elif offset < current_offset:
                raise exceptions.LayerException("Mapping returned an overlapping element")
            self._context.memory.write(layer, mapped_offset, length)
            current_offset += length

    # ## Scan implementation with knowledge of pages

    def scan(self, context, scanner, progress_callback = None, min_address = None, max_address = None):
        """Scans a Translation layer by chunk

           Note: this will skip missing/unmappable chunks of memory
        """
        min_address, max_address, scanner, total_size = self._pre_scan(context, min_address, max_address,
                                                                       progress_callback, scanner)

        progress = 0
        for block in range(min_address, total_size, scanner.chunk_size):
            for map in self.mapping(block, scanner.chunk_size + scanner.overlap, ignore_errors = True):
                offset, mapped_offset, length, layer = map
                chunk = self._context.memory.read(layer, mapped_offset, length)
                if progress_callback:
                    progress += length
                    progress_callback(progress / total_size)
                for x in scanner(chunk, offset):
                    yield x


class Memory(validity.ValidityRoutines, collections.abc.Mapping):
    """Container for multiple layers of data"""

    def __init__(self):
        self._layers = {}

    def read(self, layer, offset, length, pad = False):
        """Reads from a particular layer at offset for length bytes

           Returns 'bytes' not 'str'
        """
        return self[layer].read(offset, length, pad)

    def write(self, layer, offset, data):
        """Writes to a particular layer at offset for length bytes"""
        self[layer].write(offset, data)

    def add_layer(self, layer):
        """Adds a layer to memory model

           This will throw an exception if the required dependencies are not met
        """
        self._check_type(layer, DataLayerInterface)
        if isinstance(layer, TranslationLayerInterface):
            if layer.name in self._layers:
                raise exceptions.LayerException("Layer already exists: {}".format(layer.name))
            missing_list = [sublayer for sublayer in layer.dependencies if sublayer not in self._layers]
            if missing_list:
                raise exceptions.LayerException(
                    "Layer {} has unmet dependencies: {}".format(layer.name, ", ".join(missing_list)))
        self._layers[layer.name] = layer

    def del_layer(self, name):
        """Removes the layer called name

           This will throw an exception if other layers depend upon this layer
        """
        for layer in self._layers:
            depend_list = [superlayer for superlayer in self._layers if name in superlayer.dependencies]
            if depend_list:
                raise exceptions.LayerException(
                    "Layer {} is depended upon: {}".format(layer.name, ", ".join(depend_list)))
        self._layers[name].destroy()
        del self._layers[name]

    def free_layer_name(self, prefix = "layer"):
        """Returns an unused layer name"""
        self._check_type(prefix, str)

        count = 1
        while prefix + str(count) in self:
            count += 1
        return prefix + str(count)

    def __getitem__(self, name):
        """Returns the layer of specified name"""
        return self._layers[name]

    def __len__(self):
        return len(self._layers)

    def __iter__(self):
        return iter(self._layers)

    def check_cycles(self):
        """Runs through the available layers and identifies if there are cycles in the DAG"""
        # TODO: Is having a cycle check necessary?
