# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

"""Register backend ops in mxnet.symbol namespace."""
__all__ = []

import sys as _sys
import os as _os
import ctypes
import numpy as _numpy  # pylint: disable=unused-import

from ..base import mx_uint, check_call, _LIB, py_str
from ..symbol_doc import _build_doc

# Use different version of SymbolBase
# When possible, use cython to speedup part of computation.
# pylint: disable=unused-import
try:
    if int(_os.environ.get("MXNET_ENABLE_CYTHON", True)) == 0:
        from .._ctypes.symbol import SymbolBase, _set_symbol_class
        from .._ctypes.symbol import _symbol_creator
    elif _sys.version_info >= (3, 0):
        from .._cy3.symbol import SymbolBase, _set_symbol_class
        from .._cy3.symbol import _symbol_creator
    else:
        from .._cy2.symbol import SymbolBase, _set_symbol_class
        from .._cy2.symbol import _symbol_creator
except ImportError:
    if int(_os.environ.get("MXNET_ENFORCE_CYTHON", False)) != 0:
        raise ImportError("Cython Module cannot be loaded but MXNET_ENFORCE_CYTHON=1")
    from .._ctypes.symbol import SymbolBase, _set_symbol_class
    from .._ctypes.symbol import _symbol_creator

from ..base import _Null, _init_op_module
from ..name import NameManager
from ..attribute import AttrScope
# pylint: enable=unused-import


def _make_atomic_symbol_function(handle, name):
    """Create an atomic symbol function by handle and function name."""
    real_name = ctypes.c_char_p()
    desc = ctypes.c_char_p()
    num_args = mx_uint()
    arg_names = ctypes.POINTER(ctypes.c_char_p)()
    arg_types = ctypes.POINTER(ctypes.c_char_p)()
    arg_descs = ctypes.POINTER(ctypes.c_char_p)()
    key_var_num_args = ctypes.c_char_p()
    ret_type = ctypes.c_char_p()

    check_call(_LIB.MXSymbolGetAtomicSymbolInfo(
        handle, ctypes.byref(real_name), ctypes.byref(desc),
        ctypes.byref(num_args),
        ctypes.byref(arg_names),
        ctypes.byref(arg_types),
        ctypes.byref(arg_descs),
        ctypes.byref(key_var_num_args),
        ctypes.byref(ret_type)))
    narg = int(num_args.value)
    arg_names = [py_str(arg_names[i]) for i in range(narg)]
    arg_types = [py_str(arg_types[i]) for i in range(narg)]
    func_name = name
    key_var_num_args = py_str(key_var_num_args.value)
    ret_type = py_str(ret_type.value) if ret_type.value is not None else ''
    doc_str = _build_doc(func_name,
                         py_str(desc.value),
                         arg_names,
                         arg_types,
                         [py_str(arg_descs[i]) for i in range(narg)],
                         key_var_num_args,
                         ret_type)

    dtype_name = None
    arr_name = None
    ndsignature = []
    signature = []
    ndarg_names = []
    kwarg_names = []
    for i in range(narg):
        name, atype = arg_names[i], arg_types[i]
        if name == 'dtype':
            dtype_name = name
            signature.append('%s=_Null'%name)
        elif atype.startswith('NDArray') or atype.startswith('Symbol'):
            assert not arr_name, \
                "Op can only have one argument with variable " \
                "size and it must be the last argument."
            if atype.endswith('[]'):
                ndsignature.append('*%s'%name)
                arr_name = name
            else:
                ndsignature.append('%s=None'%name)
                ndarg_names.append(name)
        else:
            signature.append('%s=_Null'%name)
            kwarg_names.append(name)
    #signature.append('is_train=False')
    signature.append('name=None')
    signature.append('attr=None')
    signature.append('out=None')
    signature.append('**kwargs')
    signature = ndsignature + signature

    code = []
    if arr_name:
        code.append("""
def %s(*%s, **kwargs):"""%(func_name, arr_name))
        code.append("""
    sym_args = []
    for i in {}:
        assert isinstance(i, SymbolBase), \\
            "Positional arguments must be Symbol instances, " \\
            "but got %s"%str(i)
        sym_args.append(i)""".format(arr_name))
        if dtype_name is not None:
            code.append("""
    if '%s' in kwargs:
        kwargs['%s'] = _numpy.dtype(kwargs['%s']).name"""%(
            dtype_name, dtype_name, dtype_name))
        code.append("""
    attr = kwargs.pop('attr', None)
    kwargs.update(AttrScope.current.get(attr))
    name = kwargs.pop('name', None)
    name = NameManager.current.get(name, '%s')
    _ = kwargs.pop('out', None)
    keys = []
    vals = []
    sym_kwargs = dict()
    for k, v in kwargs.items():
        if isinstance(v, SymbolBase):
            sym_kwargs[k] = v
        else:
            keys.append(k)
            vals.append(v)"""%(func_name.lower()))
        if key_var_num_args:
            code.append("""
    if '%s' not in kwargs:
        keys.append('%s')
        vals.append(len(sym_args) + len(sym_kwargs))"""%(
            key_var_num_args, key_var_num_args))

        code.append("""
    return _symbol_creator(%d, sym_args, sym_kwargs, keys, vals, name)"""%(
        handle.value))
    else:
        code.append("""
def %s(%s):
    kwargs.update(AttrScope.current.get(attr))
    sym_kwargs = dict()
    keys = []
    vals = []"""%(func_name, ', '.join(signature)))
        code.append("""
    for k, v in kwargs.items():
        if isinstance(v, SymbolBase):
            sym_kwargs[k] = v
        else:
            keys.append(k)
            vals.append(v)""")
        # NDArray args
        for name in ndarg_names: # pylint: disable=redefined-argument-from-local
            code.append("""
    if {name} is not None:
        assert isinstance({name}, SymbolBase), \\
            "Argument {name} must be Symbol instances, but got %s"%str({name})
        sym_kwargs['{name}'] = {name}""".format(name=name))
        # kwargs
        for name in kwarg_names: # pylint: disable=redefined-argument-from-local
            code.append("""
    if %s is not _Null:
        keys.append('%s')
        vals.append(%s)"""%(name, name, name))
        # dtype
        if dtype_name is not None:
            code.append("""
    if %s is not _Null:
        keys.append('%s')
        vals.append(_numpy.dtype(%s).name)"""%(dtype_name, dtype_name, dtype_name))

        code.append("""
    name = NameManager.current.get(name, '%s')
    return _symbol_creator(%d, None, sym_kwargs, keys, vals, name)"""%(
        func_name.lower(), handle.value))

    local = {}
    exec(''.join(code), None, local)  # pylint: disable=exec-used
    symbol_function = local[func_name]
    symbol_function.__name__ = func_name
    symbol_function.__doc__ = doc_str
    symbol_function.__module__ = 'mxnet.symbol'
    return symbol_function


_init_op_module('mxnet', 'symbol', _make_atomic_symbol_function)
