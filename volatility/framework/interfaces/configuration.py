import collections
import copy
import json
import logging
import sys
from abc import ABCMeta, abstractmethod

from volatility.framework import validity
from volatility.framework.interfaces.context import ContextInterface

__author__ = 'mike'

CONFIG_SEPARATOR = "."

vollog = logging.getLogger(__name__)


def path_join(*args):
    """Joins the config paths together"""
    # If a path element (particularly the first) is empty, then remove it from the list
    args = [arg for arg in args if arg]
    return CONFIG_SEPARATOR.join(args)


class RequirementInterface(validity.ValidityRoutines, metaclass = ABCMeta):
    """Class to distinguish configuration elements from everything else"""

    def __init__(self, name, description = None, default = None, optional = False):
        super().__init__()
        self._check_type(name, str)
        if CONFIG_SEPARATOR in name:
            raise ValueError("Name cannot contain the config-hierarchy divider (" + CONFIG_SEPARATOR + ")")
        self._name = name
        self._description = description or ""
        self._default = default
        self._optional = optional
        self._requirements = {}

    def __repr__(self):
        return "<" + self.__class__.__name__ + ": " + self.name + ">"

    @property
    def name(self):
        """The name of the Option."""
        return self._name

    @property
    def description(self):
        """A short description of what the Option is designed to affect or achieve."""
        return self._description

    @property
    def default(self):
        """Returns the default value if one is set"""
        return self._default

    @property
    def optional(self):
        """Whether the option is required for or not"""
        return self._optional

    def config_value(self, context, config_path, default = None):
        """Returns the value for this element from its config path"""
        return context.config.get(path_join(config_path, self.name), default)

    # Child operations
    @property
    def requirements(self):
        """Returns a dictionary of all the child requirements, indexed by name"""
        return self._requirements.copy()

    def add_requirement(self, requirement):
        """Adds a child to the list of requirements"""
        self._check_type(requirement, RequirementInterface)
        self._requirements[requirement.name] = requirement

    def remove_requirement(self, requirement):
        """Removes a child from the list of requirements"""
        self._check_type(requirement, RequirementInterface)
        del self._requirements[requirement.name]

    def validate_children(self, context, config_path):
        """Method that will validate all child requirements"""
        return all([requirement.validate(context, path_join(config_path, self._name)) for requirement in
                    self.requirements.values() if not requirement.optional])

    # Validation routines
    @abstractmethod
    def validate(self, context, config_path):
        """Method to validate the value stored at config_path for the configuration object against a context

           Returns False when an item is invalid
        """


class ClassRequirement(RequirementInterface):
    """Requires a specific class"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._cls = None

    @property
    def cls(self):
        return self._cls

    def validate(self, context, config_path):
        """Checks to see if a class can be recovered"""
        value = self.config_value(context, config_path, None)
        self._cls = None
        if value is not None:
            if "." in value:
                # TODO: consider importing the prefix
                module = sys.modules.get(value[:value.rindex(".")], None)
                class_name = value[value.rindex(".") + 1:]
                if hasattr(module, class_name):
                    self._cls = getattr(module, class_name)
            else:
                if value in globals():
                    self._cls = globals()[value]
        return self._cls is not None


class ConstructableRequirementInterface(RequirementInterface):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.add_requirement(ClassRequirement("class", "Class of the translation layer"))
        self._current_class_requirements = set()

    @abstractmethod
    def construct(self, context, config_path):
        """Method for constructing within the context any required elements from subrequirements"""

    def _check_class(self, context, config_path):
        """Method to check if the class Requirement is valid and if so populate the other requirements
           (but no need to validate, since we're invalid already)
        """
        class_req = self.requirements['class']
        subreq_config_path = path_join(config_path, self.name)
        if class_req.validate(context, subreq_config_path):
            # We have a class, and since it's validated we can construct our requirements from it
            if issubclass(class_req.cls, ConfigurableInterface):
                # In case the class has changed, clear out the old requirements
                for old_req in self._current_class_requirements.copy():
                    del self._requirements[old_req]
                    self._current_class_requirements.remove(old_req)
                # And add the new ones
                for requirement in class_req.cls.get_requirements():
                    self._current_class_requirements.add(requirement.name)
                    self.add_requirement(requirement)

    def _construct_class(self, context, config_path, requirement_dict = None):
        """Constructs the class, handing args and the subrequirements as parameters to __init__"""
        cls = self.requirements["class"].cls

        # These classes all have a name property
        # We could subclass this out as a NameableInterface, but it seems a little excessive
        # FIXME: We can't test this, because importing the other interfaces causes all kinds of import loops
        # if not issubclass(cls, [interfaces.layers.TranslationLayerInterface,
        #                         interfaces.symbols.SymbolTableInterface]):
        #     return None

        if requirement_dict is None:
            requirement_dict = {}

        # Fulfillment must happen, exceptions happening here mean the requirements aren't correct
        # and these need to be raised and fixed, rather than caught and ignored
        obj = cls(**requirement_dict)
        context.config[config_path] = obj.name
        return obj


class ConfigurableInterface(validity.ValidityRoutines, metaclass = ABCMeta):
    """Class to allow objects to have requirements and read configuration data from the context config tree"""

    def __init__(self, context, config_path):
        """Basic initializer that allows configurables to access their own config settings"""
        super().__init__()
        self._context = self._check_type(context, ContextInterface)
        self._config_path = self._check_type(config_path, str)

        # Store these programmatically, so we don't keep repreating the requirements
        # This also allows constructed objects to populate a configuration without too much trouble
        for requirement in self.get_requirements():
            # Create the (private) properties using the config as backend storage
            setattr(self, "_" + requirement.name, self.config.get(requirement.name, requirement.default))

    @property
    def context(self):
        return self._context

    @property
    def config_path(self):
        return self._config_path

    @config_path.setter
    def config_path(self, value):
        self._config_path = self._check_type(value, str)

    @property
    def config(self):
        return self._context.config.branch(self._config_path)

    def build_configuration(self):
        """Constructs a HierarchicalDictionary of all the options required to build this component in the current context.

           Ensures that if the class has been created, it can be recreated using the configuration built
           Inheriting classes must override this to ensure any dependent classes update their configurations too
        """
        result = HierarchicalDict()
        for req in self.get_requirements():
            value = getattr(self, "_" + req.name, req.default)
            if value is not None:
                result[req.name] = value
        return result

    @classmethod
    def get_requirements(cls):
        """Returns a list of RequirementInterface objects required by this object"""
        return []

    @classmethod
    def validate(cls, context, config_path):
        return all([requirement.validate(context, config_path) for requirement in cls.get_requirements() if
                    not requirement.optional])


class HierarchicalDict(collections.Mapping):
    def __init__(self, initial_dict = None, separator = CONFIG_SEPARATOR):
        if not (isinstance(separator, str) and len(separator) == 1):
            raise TypeError("Separator must be a one character string")
        self._separator = separator
        self._data = {}
        self._subdict = {}
        if isinstance(initial_dict, str):
            initial_dict = json.loads(initial_dict)
        if isinstance(initial_dict, dict):
            for k, v in initial_dict.items():
                self[k] = v
        elif initial_dict is not None:
            raise TypeError("Initial_dict must be a dictionary or JSON string containing a dictionary")

    @property
    def separator(self):
        return self._separator

    @property
    def data(self):
        return self._data.copy()

    def _key_head(self, key):
        """Returns the first division of a key based on the dict separator,
           or the full key if the separator is not present
        """
        if self.separator in key:
            return key[:key.index(self.separator)]
        else:
            return key

    def _key_tail(self, key):
        """Returns all but the first division of a key based on the dict separator,
           or None if the separator is not in the key
        """
        if self.separator in key:
            return key[key.index(self.separator) + 1:]
        return None

    def __iter__(self):
        """Returns an iterator object that supports the iterator protocol"""
        return self.generator()

    def generator(self):
        """Yields the next element in the iterator"""
        for key in self._data:
            yield key
        for subdict_key in self._subdict:
            for key in self._subdict[subdict_key]:
                yield subdict_key + self.separator + key

    def __getitem__(self, key):
        """Gets an item, traversing down the trees to get to the final value"""
        try:
            if self.separator in key:
                subdict = self._subdict[self._key_head(key)]
                return subdict[self._key_tail(key)]
            else:
                return self._data[key]
        except KeyError:
            raise KeyError(key)

    def __setitem__(self, key, value):
        """Sets an item or creates a subdict and sets the item within that"""
        self._setitem(key, value)

    def _setitem(self, key, value, is_data = True):
        """Set an item or appends a whole subtree at a key location"""
        if self.separator in key:
            subdict = self._subdict.get(self._key_head(key), HierarchicalDict(separator = self.separator))
            subdict[self._key_tail(key)] = value
            self._subdict[self._key_head(key)] = subdict
        else:
            if is_data:
                self._data[key] = value
            else:
                if not isinstance(value, HierarchicalDict) and value is not None:
                    raise TypeError("HierarchicalDicts can only store HierarchicalDicts within their structure")
                self._subdict[key] = value

    def __delitem__(self, key):
        """Deletes an item from the hierarchical dict"""
        try:
            if self.separator in key:
                subdict = self._subdict[self._key_head(key)]
                del subdict[self._key_tail(key)]
                if not subdict:
                    del self._subdict[self._key_head(key)]
        except KeyError:
            raise KeyError(key)

    def __contains__(self, key):
        """Determines whether the key is present in the hierarchy"""
        if self.separator in key:
            try:
                subdict = self._subdict[self._key_head(key)]
                return self._key_tail(key) in subdict
            except KeyError:
                return False
        else:
            return key in self._data

    def __len__(self):
        """Returns the length of all items"""
        return len(self._data) + sum([len(subdict) for subdict in self._subdict])

    def branch(self, key):
        """Returns the HierarchicalDict housed under the key"""
        if self.separator in key:
            return self._subdict[self._key_head(key)].branch(self._key_tail(key))
        else:
            return self._subdict[key]

    def splice(self, key, value):
        """Splices an existing HierarchicalDictionary under a key"""
        if not isinstance(key, str) or not isinstance(value, HierarchicalDict):
            raise TypeError("Splice requires a string key and HierarchicalDict value")
        self._setitem(key, value, False)

    def clone(self):
        """Duplicate the configuration, allowing changes without affecting the original"""
        return copy.deepcopy(self)

    def __str__(self):
        """Turns the Hierarchical dict into a string representation"""
        return json.dumps(dict([(key, self[key]) for key in self.generator()]), indent = 2)


class TranslationLayerRequirement(ConstructableRequirementInterface):
    """Class maintaining the limitations on what sort of address spaces are acceptable"""

    def __init__(self, name, description = None, default = None, optional = False):
        """Constructs a Translation Layer Requirement

        The configuration option's value will be the name of the layer once it exists in the store

        :param name: Name of the configuration requirement
        :param layer_name: String detailing the expected name of the required layer, this can be None if it is to be randomly generated
        :return:
        """
        super().__init__(name, description, default, optional)

    # TODO: Add requirements: acceptable OSes from the address_space information
    # TODO: Add requirements: acceptable arches from the available layers

    def validate(self, context, config_path):
        """Validate that the value is a valid layer name and that the layer adheres to the requirements"""
        value = self.config_value(context, config_path, None)
        if isinstance(value, str):
            if value not in context.memory:
                vollog.debug("IndexError - Layer " + value + " not found in memory space")
                return False
            return True

        if value is not None:
            vollog.debug("TypeError - TranslationLayerRequirements only accepts string labels")
            return False

        # TODO: check that the space in the context lives up to the requirements for arch/os etc

        ### NOTE: This validate method has side effects (the dependencies can change)!!!

        self._check_class(context, config_path)
        vollog.debug("IndexError - No configuration provided for layer")
        return False

    def construct(self, context, config_path):
        """Constructs the appropriate layer and adds it based on the class parameter"""
        # Determine the layer name
        name = self.name
        counter = 2
        while name in context.memory:
            name = self.name + str(counter)
            counter += 1

        config_path = path_join(config_path, self.name)

        args = {"context": context,
                "config_path": config_path,
                "name": name}

        if not all([subreq.validate(context, config_path) for subreq in self.requirements.values() if
                    not subreq.optional]):
            return False

        obj = self._construct_class(context, config_path, args)
        if obj is None:
            return False
        context.add_layer(obj)
        return True


class SymbolRequirement(ConstructableRequirementInterface):
    """Class maintaining the limitations on what sort of symbol spaces are acceptable"""

    def validate(self, context, config_path):
        """Validate that the value is a valid within the symbol space of the provided context"""
        value = self.config_value(context, config_path, None)
        if not isinstance(value, str):
            vollog.debug("TypeError - SymbolRequirement only accepts string labels")
            return False
        if value not in context.symbol_space:
            # This is an expected situation, so return False rather than raise
            vollog.debug("IndexError - " + (value or "") + " is not present in the symbol space")
            return False
        return True

    def construct(self, context, config_path):
        """Constructs the symbol space within the context based on the subrequirements"""
        # Determine the space name
        name = self.name
        if name in context.symbol_space:
            raise ValueError("Symbol space already contains a SymbolTable by the same name")

        config_path = path_join(config_path, self.name)
        args = {"context": context,
                "config_path": config_path,
                "name": name}

        if not all([subreq.validate(context, config_path) for subreq in self.requirements.values() if
                    not subreq.optional]):
            return False

        # Fill out the parameter for class creation
        cls = self.requirements["class"].cls
        node_config = context.config.branch(config_path)
        for req in cls.get_requirements():
            if req.name in node_config.data and req.name != "class":
                args[req.name] = node_config.data[req.name]

        obj = self._construct_class(context, config_path, args)
        if obj is None:
            return False
        context.symbol_space.append(obj)
        return True
