from abc import abstractproperty

from jedi._compatibility import is_py3
from jedi.common import unite
from jedi import debug
from jedi.evaluate import compiled
from jedi.evaluate import filters
from jedi.evaluate.context import Context, LazyKnownContext, LazyKnownContexts
from jedi.evaluate.cache import memoize_default
from jedi.cache import memoize_method
from jedi.evaluate import representation as er
from jedi.evaluate.dynamic import search_params


class AbstractInstanceContext(Context):
    """
    This class is used to evaluate instances.
    """
    def __init__(self, evaluator, parent_context, class_context, var_args):
        super(AbstractInstanceContext, self).__init__(evaluator, parent_context)
        # Generated instances are classes that are just generated by self
        # (No var_args) used.
        self.class_context = class_context
        self.var_args = var_args

        #####
        """"
        if class_context.name.string_name in ['list', 'set'] \
                and evaluator.BUILTINS == parent_context.get_root_context():
            # compare the module path with the builtin name.
            self.var_args = iterable.check_array_instances(evaluator, self)
        elif not is_generated:
            # Need to execute the __init__ function, because the dynamic param
            # searching needs it.
            try:
                method = self.get_subscope_by_name('__init__')
            except KeyError:
                pass
            else:
                self._init_execution = evaluator.execute(method, self.var_args)
        """

    def is_class(self):
        return False

    @property
    def py__call__(self):
        names = self.get_function_slot_names('__call__')
        if not names:
            # Means the Instance is not callable.
            raise AttributeError

        def execute(arguments):
            return unite(name.execute(arguments) for name in names)

        return execute

    def py__class__(self):
        return self.class_context

    def py__bool__(self):
        # Signalize that we don't know about the bool type.
        return None

    def get_function_slot_names(self, name):
        # Python classes don't look at the dictionary of the instance when
        # looking up `__call__`. This is something that has to do with Python's
        # internal slot system (note: not __slots__, but C slots).
        for filter in self.get_filters(include_self_names=False):
            names = filter.get(name)
            if names:
                return names
        return []

    def execute_function_slots(self, names, *evaluated_args):
        return unite(
            name.execute_evaluated(*evaluated_args)
            for name in names
        )

    def get_descriptor_returns(self, obj):
        """ Throws a KeyError if there's no method. """
        # Arguments in __get__ descriptors are obj, class.
        # `method` is the new parent of the array, don't know if that's good.
        names = self.get_function_slot_names('__get__')
        if names:
            if isinstance(obj, AbstractInstanceContext):
                return self.execute_function_slots(names, obj, obj.class_context)
            else:
                none_obj = compiled.create(self.evaluator, None)
                return self.execute_function_slots(names, none_obj, obj)
        else:
            return set([self])

    def get_filters(self, search_global=None, until_position=None,
                    origin_scope=None, include_self_names=True):
        if include_self_names:
            for cls in self.class_context.py__mro__():
                if isinstance(cls, compiled.CompiledObject):
                    yield SelfNameFilter(self.evaluator, self, cls, origin_scope)
                else:
                    yield SelfNameFilter(self.evaluator, self, cls, origin_scope)

        for cls in self.class_context.py__mro__():
            if isinstance(cls, compiled.CompiledObject):
                yield CompiledInstanceClassFilter(self.evaluator, self, cls)
            else:
                yield InstanceClassFilter(self.evaluator, self, cls, origin_scope)

    def py__getitem__(self, index):
        try:
            names = self.get_function_slot_names('__getitem__')
        except KeyError:
            debug.warning('No __getitem__, cannot access the array.')
            return set()
        else:
            index_obj = compiled.create(self.evaluator, index)
            return unite(name.execute_evaluated(index_obj) for name in names)

    def py__iter__(self):
        iter_slot_names = self.get_function_slot_names('__iter__')
        if not iter_slot_names:
            debug.warning('No __iter__ on %s.' % self)
            return

        for generator in self.execute_function_slots(iter_slot_names):
            if isinstance(generator, AbstractInstanceContext):
                # `__next__` logic.
                name = '__next__' if is_py3 else 'next'
                iter_slot_names = generator.get_function_slot_names(name)
                if iter_slot_names:
                    yield LazyKnownContexts(
                        generator.execute_function_slots(iter_slot_names)
                    )
                else:
                    debug.warning('Instance has no __next__ function in %s.', generator)
            else:
                for lazy_context in generator.py__iter__():
                    yield lazy_context

    @abstractproperty
    def name(self):
        pass

    def __repr__(self):
        return "<%s of %s(%s)>" % (self.__class__.__name__, self.class_context,
                                   self.var_args)


class CompiledInstance(AbstractInstanceContext):
    @property
    def name(self):
        return compiled.CompiledContextName(self, self.class_context.name.string_name)


class TreeInstance(AbstractInstanceContext):
    @property
    def name(self):
        return filters.ContextName(self, self.class_context.name.tree_name)

    @memoize_default()
    def create_instance_context(self, class_context, node):
        scope = node.get_parent_scope()
        if scope == class_context.classdef:
            return class_context
        else:
            parent_context = self.create_instance_context(class_context, scope)
            if scope.type == 'funcdef':
                if scope.name.value == '__init__' and parent_context == class_context:
                    return InstanceFunctionExecution(
                        self,
                        class_context.parent_context,
                        scope,
                        self.var_args
                    )
                else:
                    return AnonymousInstanceFunctionExecution(
                        self,
                        class_context.parent_context,
                        scope,
                    )
            else:
                raise NotImplementedError
        return class_context


class AnonymousInstance(TreeInstance):
    def __init__(self, evaluator, parent_context, class_context):
        super(AnonymousInstance, self).__init__(
            evaluator,
            parent_context,
            class_context,
            var_args=None
        )


class CompiledInstanceClassFilter(compiled.CompiledObjectFilter):
    def __init__(self, evaluator, instance, compiled_object):
        super(CompiledInstanceClassFilter, self).__init__(
            evaluator,
            compiled_object,
            is_instance=True,
        )
        self._instance = instance

    def _filter(self, names):
        names = super(CompiledInstanceClassFilter, self)._filter(names)
        return [get_instance_el(self._evaluator, self._instance, name, True)
                for name in names]


class BoundMethod(Context):
    def __init__(self, instance, class_context, function):
        self._instance = instance
        self._class_context = class_context
        self._function = function

    def __getattr__(self, name):
        return getattr(self._function, name)

    def py__call__(self, var_args):
        function_execution = InstanceFunctionExecution(
            self._instance,
            self.parent_context,
            self._function.funcdef,
            var_args
        )
        return self._function.infer_function_execution(function_execution)

    def __repr__(self):
        return '<%s: %s>' % (self.__class__.__name__, self._function)


class InstanceNameDefinition(filters.TreeNameDefinition):
    def infer(self):
        contexts = super(InstanceNameDefinition, self).infer()
        for context in contexts:
            yield context


class LazyInstanceName(filters.TreeNameDefinition):
    """
    This name calculates the parent_context lazily.
    """
    def __init__(self, instance, class_context, tree_name):
        self._instance = instance
        self._class_context = class_context
        self.tree_name = tree_name

    @property
    def parent_context(self):
        return self._instance.create_instance_context(self._class_context, self.tree_name)


class LazyInstanceClassName(LazyInstanceName):
    def infer(self):
        for v in super(LazyInstanceClassName, self).infer():
            if isinstance(v, er.FunctionContext):
                yield BoundMethod(self._instance, self._class_context, v)
            else:
                yield v


class InstanceClassFilter(filters.ParserTreeFilter):
    name_class = LazyInstanceClassName

    def __init__(self, evaluator, context, class_context, origin_scope):
        super(InstanceClassFilter, self).__init__(
            evaluator=evaluator,
            context=context,
            parser_scope=class_context.classdef,
            origin_scope=origin_scope
        )
        self._class_context = class_context

    def _equals_origin_scope(self):
        node = self._origin_scope
        while node is not None:
            if node == self._parser_scope or node == self._context:
                return True
            node = node.get_parent_scope()
        return False

    def _access_possible(self, name):
        return not name.value.startswith('__') or name.value.endswith('__') \
            or self._equals_origin_scope()

    def _filter(self, names):
        names = super(InstanceClassFilter, self)._filter(names)
        return [name for name in names if self._access_possible(name)]

    def _check_flows(self, names):
        return names

    def _convert_names(self, names):
        return [self.name_class(self._context, self._class_context, name) for name in names]


class SelfNameFilter(InstanceClassFilter):
    name_class = LazyInstanceName

    def _filter(self, names):
        names = self._filter_self_names(names)
        if isinstance(self._parser_scope, compiled.CompiledObject):
            # This would be for builtin skeletons, which are not yet supported.
            return []
        start, end = self._parser_scope.start_pos, self._parser_scope.end_pos
        return [n for n in names if start < n.start_pos < end]

    def _filter_self_names(self, names):
        for name in names:
            trailer = name.parent
            if trailer.type == 'trailer' \
                    and len(trailer.children) == 2 \
                    and trailer.children[0] == '.':
                if name.is_definition() and self._access_possible(name):
                    yield name
                    continue
                    init_execution = self._context.get_init_function()
                    # Hopefully we can somehow change this.
                    if init_execution is not None and \
                            init_execution.start_pos < name.start_pos < init_execution.end_pos:
                        name = init_execution.name_for_position(name.start_pos)
                    yield name


class InstanceVarArgs(object):
    def __init__(self, instance, funcdef, var_args):
        self._instance = instance
        self._funcdef = funcdef
        self._var_args = var_args

    @memoize_method
    def _get_var_args(self):
        if self._var_args is None:
            # TODO this parent_context might be wrong. test?!
            return search_params(
                self._instance.evaluator,
                self._instance.class_context,
                self._funcdef
            )
        return self._var_args

    def unpack(self, func=None):
        yield None, LazyKnownContext(self._instance)
        for values in self._get_var_args().unpack(func):
            yield values

    def get_calling_var_args(self):
        return self._get_var_args().get_calling_var_args()


class InstanceFunctionExecution(er.FunctionExecutionContext):
    def __init__(self, instance, parent_context, funcdef, var_args):
        self.instance = instance
        var_args = InstanceVarArgs(instance, funcdef, var_args)

        super(InstanceFunctionExecution, self).__init__(
            instance.evaluator, parent_context, funcdef, var_args)


class AnonymousInstanceFunctionExecution(InstanceFunctionExecution):
    function_execution_filter = filters.AnonymousInstanceFunctionExecutionFilter

    def __init__(self, instance, parent_context, funcdef):
        super(AnonymousInstanceFunctionExecution, self).__init__(
            instance, parent_context, funcdef, None)
