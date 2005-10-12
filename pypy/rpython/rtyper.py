"""
RTyper: converts high-level operations into low-level operations in flow graphs.

The main class, with code to walk blocks and dispatch individual operations
to the care of the rtype_*() methods implemented in the other r* modules.
For each high-level operation 'hop', the rtype_*() methods produce low-level
operations that are collected in the 'llops' list defined here.  When necessary,
conversions are inserted.

This logic borrows a bit from pypy.translator.annrpython, without the fixpoint
computation part.
"""

from __future__ import generators
import sys, os
import py
from pypy.annotation.pairtype import pair
from pypy.annotation import model as annmodel
from pypy.objspace.flow.model import Variable, Constant, Block, Link
from pypy.objspace.flow.model import SpaceOperation, last_exception
from pypy.rpython.lltype import Signed, Unsigned, Float, Char, Bool, Void
from pypy.rpython.lltype import LowLevelType, Ptr, ContainerType
from pypy.rpython.lltype import FuncType, functionptr, typeOf, RuntimeTypeInfo
from pypy.rpython.lltype import attachRuntimeTypeInfo, Primitive
from pypy.rpython.ootype import ootype
from pypy.tool.sourcetools import func_with_new_name, valid_identifier
from pypy.translator.unsimplify import insert_empty_block
from pypy.rpython.rmodel import Repr, inputconst
from pypy.rpython.rmodel import TyperError, BrokenReprTyperError
from pypy.rpython.rmodel import getfunctionptr, getstaticmeth, warning
from pypy.rpython.normalizecalls import perform_normalizations
from pypy.rpython.annlowlevel import annotate_lowlevel_helper
from pypy.rpython.exceptiondata import ExceptionData

from pypy.rpython.rmodel import log


class RPythonTyper:

    def __init__(self, annotator, type_system="lltype"):
        self.annotator = annotator

        if type_system not in ("lltype", "ootype"):
            raise TyperError("Unknown type system %r!" % type_system)
        self.type_system = type_system

        self.reprs = {}
        self._reprs_must_call_setup = []
        self._seen_reprs_must_call_setup = {}
        self.specialized_ll_functions = {}
        self.class_reprs = {}
        self.instance_reprs = {}
        self.pbc_reprs = {}
        self.class_pbc_attributes = {}
        self.typererrors = []
        self.typererror_count = 0
        # make the primitive_to_repr constant mapping
        self.primitive_to_repr = {}
        for s_primitive, lltype in annmodel.annotation_to_ll_map:
            r = self.getrepr(s_primitive)
            self.primitive_to_repr[r.lowleveltype] = r
        self.exceptiondata = ExceptionData(self)
        try:
            self.seed = int(os.getenv('RTYPERSEED'))
            s = 'Using %d as seed for block shuffling' % self.seed
            log.info(s)
        except:
            self.seed = 0
        self.order = None
        RTYPERORDER = os.getenv('RTYPERORDER')
        if RTYPERORDER:
            order_module = RTYPERORDER.split(',')[0]
            self.order = __import__(order_module, {}, {},  ['*']).order
            s = 'Using %s.%s for order' % (self.order.__module__, self.order.__name__)
            log.info(s)
        self.crash_on_first_typeerror = True

    def add_pendingsetup(self, repr): 
        assert isinstance(repr, Repr)
        if repr in self._seen_reprs_must_call_setup: 
            #warning("ignoring already seen repr for setup: %r" %(repr,))
            return 
        self._reprs_must_call_setup.append(repr) 
        self._seen_reprs_must_call_setup[repr] = True 
        
    def getexceptiondata(self):
        return self.exceptiondata    # built at the end of specialize()

    def getrepr(self, s_obj):
        # s_objs are not hashable... try hard to find a unique key anyway
        key = s_obj.rtyper_makekey()
        assert key[0] == s_obj.__class__
        try:
            result = self.reprs[key]
        except KeyError:
            result = s_obj.rtyper_makerepr(self)
            assert not isinstance(result.lowleveltype, ContainerType), (
                "missing a Ptr in the type specification "
                "of %s:\n%r" % (s_obj, result.lowleveltype))
            self.reprs[key] = result
            self.add_pendingsetup(result)
        return result

    def binding(self, var):
        s_obj = self.annotator.binding(var, True)
        if s_obj is None:
            s_obj = annmodel.SomeObject()
        return s_obj

    def bindingrepr(self, var):
        return self.getrepr(self.binding(var))

    def specialize(self, dont_simplify_again=False, crash_on_first_typeerror = True):
        """Main entry point: specialize all annotated blocks of the program."""
        self.crash_on_first_typeerror = crash_on_first_typeerror
        # specialize depends on annotator simplifications
        if not dont_simplify_again:
            self.annotator.simplify()
        # first make sure that all functions called in a group have exactly
        # the same signature, by hacking their flow graphs if needed
        perform_normalizations(self)
        # new blocks can be created as a result of specialize_block(), so
        # we need to be careful about the loop here.
        self.already_seen = {}

        self.specialize_more_blocks()
        self.exceptiondata.make_helpers(self)
        self.specialize_more_blocks()   # for the helpers just made

    def specialize_more_blocks(self):
        while True:
            # look for blocks not specialized yet
            pending = [block for block in self.annotator.annotated
                             if block not in self.already_seen]
            if not pending:
                break
            # shuffle blocks a bit
            if self.seed:
                import random
                r = random.Random(self.seed)
                r.shuffle(pending)

            if self.order:
                tracking = self.order(self.annotator, pending)
            else:
                tracking = lambda block: None

            # specialize all blocks in the 'pending' list
            for block in pending:
                tracking(block)
                self.specialize_block(block)
                self.already_seen[block] = True
                # progress bar
                n = len(self.already_seen)
                if n % 100 == 0:
                    total = len(self.annotator.annotated)
                    if self.typererror_count:
                        error_report = " but %d errors" % self.typererror_count
                    else:
                        error_report = ''
                    log.event('specializing: %d / %d blocks   (%d%%)%s' % (
                        n, total, 100 * n // total, error_report))
            # make sure all reprs so far have had their setup() called
            self.call_all_setups()

        if self.typererrors: 
            self.dump_typererrors(to_log=True) 
            raise TyperError("there were %d error" % len(self.typererrors))
        # make sure that the return variables of all graphs are concretetype'd
        for graph in self.annotator.translator.flowgraphs.values():
            v = graph.getreturnvar()
            self.setconcretetype(v)

    def dump_typererrors(self, num=None, minimize=True, to_log=False): 
        c = 0
        bc = 0
        for err in self.typererrors[:num]: 
            c += 1
            if minimize and isinstance(err, BrokenReprTyperError): 
                bc += 1
                continue
            block, position = err.where
            func = self.annotator.annotated.get(block, None)
            if func:
                func = "(%s:%s)" %(func.__module__ or '?', func.__name__)
            else:
                func = "(?:?)"
            errmsg = ("TyperError-%d: %s" % (c, func) +
                      str(err) +
                      "\n")
            if to_log:
                log.ERROR(errmsg)
            else:
                print errmsg
        if bc:
            minmsg = "(minimized %d errors away for this dump)" % (bc,)
            if to_log:
                log.ERROR(minmsg)
            else:
                print minmsg

    def call_all_setups(self):
        # make sure all reprs so far have had their setup() called
        must_setup_more = []
        while self._reprs_must_call_setup:
            r = self._reprs_must_call_setup.pop()
            r.setup()
            must_setup_more.append(r)
        for r in must_setup_more:
            r.setup_final()

    def setconcretetype(self, v):
        assert isinstance(v, Variable)
        v.concretetype = self.bindingrepr(v).lowleveltype

    def typedconstant(self, c, using_repr=None):
        """Make a copy of the Constant 'c' and give it a concretetype."""
        assert isinstance(c, Constant)
        if using_repr is None:
            using_repr = self.bindingrepr(c)
        if not hasattr(c, 'concretetype'):
            c = inputconst(using_repr, c.value)
        else:
            if c.concretetype is not Void:
                assert typeOf(c.value) == using_repr.lowleveltype
        return c

    def setup_block_entry(self, block):
        if block.operations == () and len(block.inputargs) == 2:
            # special case for exception blocks: force them to return an
            # exception type and value in a standardized format
            v1, v2 = block.inputargs
            v1.concretetype = self.exceptiondata.lltype_of_exception_type
            v2.concretetype = self.exceptiondata.lltype_of_exception_value
            return [self.exceptiondata.r_exception_type,
                    self.exceptiondata.r_exception_value]
        else:
            # normal path
            result = []
            for a in block.inputargs:
                r = self.bindingrepr(a)
                a.concretetype = r.lowleveltype
                result.append(r)
            return result

    def specialize_block(self, block):
        # give the best possible types to the input args
        try:
            self.setup_block_entry(block)
        except TyperError, e:
            self.gottypererror(e, block, "block-entry", None)
            return  # cannot continue this block            
            

        # specialize all the operations, as far as possible
        if block.operations == ():   # return or except block
            return
        newops = LowLevelOpList(self)
        varmapping = {}
        for v in block.getvariables():
            varmapping[v] = v    # records existing Variables

        for hop in self.highlevelops(block, newops):
            try:
                hop.setup()  # this is called from here to catch TyperErrors...
                self.translate_hl_to_ll(hop, varmapping)
            except TyperError, e:
                self.gottypererror(e, block, hop.spaceop, newops)
                return  # cannot continue this block: no op.result.concretetype

        block.operations[:] = newops
        block.renamevariables(varmapping)

        pos = newops.llop_raising_exceptions
        if (pos is not None and pos != len(newops)-1):
            # this is for the case where the llop that raises the exceptions
            # is not the last one in the list.
            assert block.exitswitch == Constant(last_exception)
            noexclink = block.exits[0]
            assert noexclink.exitcase is None
            if pos == "removed":
                # the exception cannot actually occur at all.
                # See for example rspecialcase.rtype_call_specialcase().
                # We just remove all exception links.
                block.exitswitch = None
                block.exits = block.exits[:1]
            else:
                # We have to split the block in two, with the exception-catching
                # exitswitch after the llop at 'pos', and the extra operations
                # in the new part of the block, corresponding to the
                # no-exception case.  See for example test_rlist.test_indexerror
                # or test_rpbc.test_multiple_ll_one_hl_op.
                assert 0 <= pos < len(newops) - 1
                extraops = block.operations[pos+1:]
                del block.operations[pos+1:]
                insert_empty_block(self.annotator.translator,
                                   noexclink,
                                   newops = extraops)

        self.insert_link_conversions(block)

    def insert_link_conversions(self, block):
        # insert the needed conversions on the links
        can_insert_here = block.exitswitch is None and len(block.exits) == 1
        for link in block.exits:
            if link.exitcase is not None:
                if isinstance(block.exitswitch, Variable):
                    r_case = self.bindingrepr(block.exitswitch)
                else:
                    assert block.exitswitch == Constant(last_exception)
                    r_case = rclass.get_type_repr(self)
                link.llexitcase = r_case.convert_const(link.exitcase)
            else:
                link.llexitcase = None

            a = link.last_exception
            if isinstance(a, Variable):
                a.concretetype = self.exceptiondata.lltype_of_exception_type
            elif isinstance(a, Constant):
                link.last_exception = self.typedconstant(
                    a, using_repr=self.exceptiondata.r_exception_type)

            a = link.last_exc_value
            if isinstance(a, Variable):
                a.concretetype = self.exceptiondata.lltype_of_exception_value
            elif isinstance(a, Constant):
                link.last_exc_value = self.typedconstant(
                    a, using_repr=self.exceptiondata.r_exception_value)

            inputargs_reprs = self.setup_block_entry(link.target)
            newops = LowLevelOpList(self)
            newlinkargs = {}
            for i in range(len(link.args)):
                a1 = link.args[i]
                r_a2 = inputargs_reprs[i]
                if isinstance(a1, Constant):
                    link.args[i] = self.typedconstant(a1, using_repr=r_a2)
                    continue   # the Constant was typed, done
                if a1 is link.last_exception:
                    r_a1 = self.exceptiondata.r_exception_type
                elif a1 is link.last_exc_value:
                    r_a1 = self.exceptiondata.r_exception_value
                else:
                    r_a1 = self.bindingrepr(a1)
                if r_a1 == r_a2:
                    continue   # no conversion needed
                try:
                    new_a1 = newops.convertvar(a1, r_a1, r_a2)
                except TyperError, e:
                    self.gottypererror(e, block, link, newops)
                    continue # try other args
                if new_a1 != a1:
                    newlinkargs[i] = new_a1

            if newops:
                if can_insert_here:
                    block.operations.extend(newops)
                else:
                    # cannot insert conversion operations around a single
                    # link, unless it is the only exit of this block.
                    # create a new block along the link...
                    newblock = insert_empty_block(self.annotator.translator,
                                                  link,
                    # ...and store the conversions there.
                                               newops=newops)
                    link = newblock.exits[0]
            for i, new_a1 in newlinkargs.items():
                link.args[i] = new_a1

    def highlevelops(self, block, llops):
        # enumerate the HighLevelOps in a block.
        if block.operations:
            for op in block.operations[:-1]:
                yield HighLevelOp(self, op, [], llops)
            # look for exception links for the last operation
            if block.exitswitch == Constant(last_exception):
                exclinks = block.exits[1:]
            else:
                exclinks = []
            yield HighLevelOp(self, block.operations[-1], exclinks, llops)

    def translate_hl_to_ll(self, hop, varmapping):
        log.translating(hop.spaceop.opname, hop.args_s)
        resultvar = hop.dispatch()
        if hop.exceptionlinks and hop.llops.llop_raising_exceptions is None:
            raise TyperError("the graph catches %s, but the rtyper did not "
                             "take exceptions into account "
                             "(exception_is_here() not called)" % (
                [link.exitcase.__name__ for link in hop.exceptionlinks],))
        op = hop.spaceop
        if resultvar is None:
            # no return value
            if hop.s_result != annmodel.SomeImpossibleValue():
                raise TyperError("the annotator doesn't agree that '%s' "
                                 "has no return value" % op.opname)
            op.result.concretetype = Void
        else:
            assert isinstance(resultvar, (Variable, Constant))
            # for simplicity of the translate_meth, resultvar is usually not
            # op.result here.  We have to replace resultvar with op.result
            # in all generated operations.
            if hop.s_result.is_constant():
                if isinstance(resultvar, Constant) and \
                       isinstance(hop.r_result.lowleveltype, Primitive) and \
                       hop.r_result.lowleveltype is not Void:
                    assert resultvar.value == hop.s_result.const
            resulttype = resultvar.concretetype
            op.result.concretetype = hop.r_result.lowleveltype
            if op.result.concretetype != resulttype:
                raise TyperError("inconsistent type for the result of '%s':\n"
                                 "annotator says %s,\n"
                                 "whose repr is %r\n"
                                 "but rtype_%s returned %r" % (
                    op.opname, hop.s_result,
                    hop.r_result, op.opname, resulttype))
            # figure out if the resultvar is a completely fresh Variable or not
            if (isinstance(resultvar, Variable) and
                resultvar not in self.annotator.bindings and
                resultvar not in varmapping):
                # fresh Variable: rename it to the previously existing op.result
                varmapping[resultvar] = op.result
            else:
                # renaming unsafe.  Insert a 'same_as' operation...
                hop.llops.append(SpaceOperation('same_as', [resultvar],
                                                op.result))

    def gottypererror(self, e, block, position, llops):
        """Record a TyperError without crashing immediately.
        Put a 'TyperError' operation in the graph instead.
        """
        e.where = (block, position)
        self.typererror_count += 1
        if self.crash_on_first_typeerror:
            raise
        self.typererrors.append(e)
        if llops:
            c1 = inputconst(Void, Exception.__str__(e))
            llops.genop('TYPER ERROR', [c1], resulttype=Void)

    # __________ regular operations __________

    def _registeroperations(loc):
        # All unary operations
        for opname in annmodel.UNARY_OPERATIONS:
            exec py.code.compile("""
                def translate_op_%s(self, hop):
                    r_arg1 = hop.args_r[0]
                    return r_arg1.rtype_%s(hop)
                """ % (opname, opname)) in globals(), loc
        # All binary operations
        for opname in annmodel.BINARY_OPERATIONS:
            exec py.code.compile("""
                def translate_op_%s(self, hop):
                    r_arg1 = hop.args_r[0]
                    r_arg2 = hop.args_r[1]
                    return pair(r_arg1, r_arg2).rtype_%s(hop)
                """ % (opname, opname)) in globals(), loc

    _registeroperations(locals())
    del _registeroperations

    # this one is not in BINARY_OPERATIONS
    def translate_op_contains(self, hop):
        r_arg1 = hop.args_r[0]
        r_arg2 = hop.args_r[1]
        return pair(r_arg1, r_arg2).rtype_contains(hop)

    def translate_op_hardwired_simple_call(self, hop):
        return hop.args_r[0].rtype_hardwired_simple_call(hop)

    def translate_op_hardwired_call_args(self, hop):
        return hop.args_r[0].rtype_hardwired_call_args(hop)

    # __________ irregular operations __________

    def translate_op_newlist(self, hop):
        return rlist.rtype_newlist(hop)

    def translate_op_newdict(self, hop):
        return rdict.rtype_newdict(hop)

    def translate_op_alloc_and_set(self, hop):
        return rlist.rtype_alloc_and_set(hop)

    def translate_op_newtuple(self, hop):
        return rtuple.rtype_newtuple(hop)

    def translate_op_newslice(self, hop):
        return rslice.rtype_newslice(hop)

    def translate_op_call_memo(self, hop):
        return rpbc.rtype_call_memo(hop)

    def translate_op_call_specialcase(self, hop):
        return rspecialcase.rtype_call_specialcase(hop)

    def missing_operation(self, hop):
        raise TyperError("unimplemented operation: '%s'" % hop.spaceop.opname)

    # __________ utilities __________

    def needs_hash_support(self, cls):
        return cls in self.annotator.bookkeeper.needs_hash_support

    def deref(self, obj):
        """Derefernce obj if it is a pointer."""
        if self.type_system == "ootype":
            assert isinstance(typeOf(obj), ootype.OOType)
            return obj
        elif self.type_system == "lltype":
            assert isinstance(typeOf(obj), Ptr)

            return obj._obj

    def getcallable(self, graphfunc):
        if self.type_system == "lltype":
            return self.getfunctionptr(graphfunc)
        
        elif self.type_system == "ootype":
            return self.getstaticmeth(graphfunc)

    def getstaticmeth(self, graphfunc):
        def getconcretetype(v):
            return self.bindingrepr(v).lowleveltype
        return getstaticmeth(self.annotator.translator, graphfunc, getconcretetype)

    def getfunctionptr(self, graphfunc):
        def getconcretetype(v):
            return self.bindingrepr(v).lowleveltype
        return getfunctionptr(self.annotator.translator, graphfunc, getconcretetype)

    def annotate_helper(self, ll_function, arglltypes):
        """Annotate the given low-level helper function
        and return it as a function pointer object.
        """
        args_s = [annmodel.lltype_to_annotation(T) for T in arglltypes]
        was_frozen = self.annotator.translator.frozen
        self.annotator.translator.frozen = False   # oh well
        try:
            ignored, spec_function = annotate_lowlevel_helper(self.annotator,
                                                            ll_function, args_s)
        finally:
            self.annotator.translator.frozen = was_frozen
        return self.getfunctionptr(spec_function)

    def attachRuntimeTypeInfoFunc(self, GCSTRUCT, func, ARG_GCSTRUCT=None):
        self.call_all_setups()  # compute ForwardReferences now
        if ARG_GCSTRUCT is None:
            ARG_GCSTRUCT = GCSTRUCT
        args_s = [annmodel.SomePtr(Ptr(ARG_GCSTRUCT))]
        s, spec_function = annotate_lowlevel_helper(self.annotator,
                                                    func, args_s)
        if (not isinstance(s, annmodel.SomePtr) or
            s.ll_ptrtype != Ptr(RuntimeTypeInfo)):
            raise TyperError("runtime type info function %r returns %r, "
                             "excepted Ptr(RuntimeTypeInfo)" % (func, s))
        funcptr = self.getfunctionptr(spec_function)
        attachRuntimeTypeInfo(GCSTRUCT, funcptr)

# ____________________________________________________________


class HighLevelOp(object):

    def __init__(self, rtyper, spaceop, exceptionlinks, llops):
        self.rtyper         = rtyper
        self.spaceop        = spaceop
        self.exceptionlinks = exceptionlinks
        self.llops          = llops

    def setup(self):
        rtyper = self.rtyper
        spaceop = self.spaceop
        self.nb_args  = len(spaceop.args)
        self.args_v   = list(spaceop.args)
        self.args_s   = [rtyper.binding(a) for a in spaceop.args]
        self.s_result = rtyper.binding(spaceop.result)
        self.args_r   = [rtyper.getrepr(s_a) for s_a in self.args_s]
        self.r_result = rtyper.getrepr(self.s_result)
        rtyper.call_all_setups()  # compute ForwardReferences now

    def copy(self):
        result = HighLevelOp(self.rtyper, self.spaceop,
                             self.exceptionlinks, self.llops)
        for key, value in self.__dict__.items():
            if type(value) is list:     # grunt
                value = value[:]
            setattr(result, key, value)
        return result

    def dispatch(self, opname=None):
        if not opname:
            opname = self.spaceop.opname
        rtyper = self.rtyper
        translate_meth = getattr(rtyper, 'translate_op_'+opname,
                                 rtyper.missing_operation)
        return translate_meth(self)

    def inputarg(self, converted_to, arg):
        """Returns the arg'th input argument of the current operation,
        as a Variable or Constant converted to the requested type.
        'converted_to' should be a Repr instance or a Primitive low-level
        type.
        """
        if not isinstance(converted_to, Repr):
            converted_to = self.rtyper.primitive_to_repr[converted_to]
        v = self.args_v[arg]
        if isinstance(v, Constant):
            return inputconst(converted_to, v.value)
        assert hasattr(v, 'concretetype')

        s_binding = self.args_s[arg]
        if s_binding.is_constant():
            return inputconst(converted_to, s_binding.const)

        r_binding = self.args_r[arg]
        return self.llops.convertvar(v, r_binding, converted_to)

    inputconst = staticmethod(inputconst)    # export via the HighLevelOp class

    def inputargs(self, *converted_to):
        if len(converted_to) != self.nb_args:
            raise TyperError("operation argument count mismatch:\n"
                             "'%s' has %d arguments, rtyper wants %d" % (
                self.spaceop.opname, self.nb_args, len(converted_to)))
        vars = []
        for i in range(len(converted_to)):
            vars.append(self.inputarg(converted_to[i], i))
        return vars

    def genop(self, opname, args_v, resulttype=None):
        return self.llops.genop(opname, args_v, resulttype)

    def gendirectcall(self, ll_function, *args_v):
        return self.llops.gendirectcall(ll_function, *args_v)

    def r_s_popfirstarg(self):
        "Return and discard the first argument."
        self.nb_args -= 1
        self.args_v.pop(0)
        return self.args_r.pop(0), self.args_s.pop(0)

    def v_s_insertfirstarg(self, v_newfirstarg, s_newfirstarg):
        r_newfirstarg = self.rtyper.getrepr(s_newfirstarg)
        self.args_v.insert(0, v_newfirstarg)
        self.args_r.insert(0, r_newfirstarg)
        self.args_s.insert(0, s_newfirstarg)
        self.nb_args += 1

    def swap_fst_snd_args(self):
        self.args_v[0], self.args_v[1] = self.args_v[1], self.args_v[0]
        self.args_s[0], self.args_s[1] = self.args_s[1], self.args_s[0]
        self.args_r[0], self.args_r[1] = self.args_r[1], self.args_r[0]

    def has_implicit_exception(self, exc_cls):
        if self.llops.llop_raising_exceptions is not None:
            raise TyperError("already generated the llop that raises the "
                             "exception")
        if not self.exceptionlinks:
            return False  # don't record has_implicit_exception checks on
                          # high-level ops before the last one in the block
        if self.llops.implicit_exceptions_checked is None:
            self.llops.implicit_exceptions_checked = []
        result = False
        for link in self.exceptionlinks:
            if issubclass(exc_cls, link.exitcase):
                self.llops.implicit_exceptions_checked.append(link.exitcase)
                result = True
                # go on looping to add possibly more exceptions to the list
                # (e.g. Exception itself - see test_rlist.test_valueerror)
        return result

    def exception_is_here(self):
        if self.llops.llop_raising_exceptions is not None:
            raise TyperError("cannot catch an exception at more than one llop")
        if not self.exceptionlinks:
            return # ignored for high-level ops before the last one in the block
        if self.llops.implicit_exceptions_checked is not None:
            # sanity check: complain if an has_implicit_exception() check is
            # missing in the rtyper.
            for link in self.exceptionlinks:
                if link.exitcase not in self.llops.implicit_exceptions_checked:
                    raise TyperError("the graph catches %s, but the rtyper "
                                     "did not explicitely handle it" % (
                        link.exitcase.__name__,))
        self.llops.llop_raising_exceptions = len(self.llops)

    def exception_cannot_occur(self):
        if self.llops.llop_raising_exceptions is not None:
            raise TyperError("cannot catch an exception at more than one llop")
        if not self.exceptionlinks:
            return # ignored for high-level ops before the last one in the block
        self.llops.llop_raising_exceptions = "removed"

# ____________________________________________________________

class LowLevelOpList(list):
    """A list with gen*() methods to build and append low-level
    operations to it.
    """
    # NB. the following two attributes are here instead of on HighLevelOp
    #     because we want them to be shared between a HighLevelOp and its
    #     copy()es.
    llop_raising_exceptions = None
    implicit_exceptions_checked = None

    def __init__(self, rtyper):
        self.rtyper = rtyper

    def convertvar(self, v, r_from, r_to):
        assert isinstance(v, (Variable, Constant))
        if r_from != r_to:
            v = pair(r_from, r_to).convert_from_to(v, self)
            if v is NotImplemented:
                raise TyperError("don't know how to convert from %r to %r" %
                                 (r_from, r_to))
            if v.concretetype != r_to.lowleveltype:
                raise TyperError("bug in convertion from %r to %r: "
                                 "returned a %r" % (r_from, r_to,
                                                    v.concretetype))
        return v

    def genop(self, opname, args_v, resulttype=None):
        vresult = Variable()
        self.append(SpaceOperation(opname, args_v, vresult))
        if resulttype is None:
            vresult.concretetype = Void
            return None
        else:
            if isinstance(resulttype, Repr):
                resulttype = resulttype.lowleveltype
            assert isinstance(resulttype, LowLevelType)
            vresult.concretetype = resulttype
            return vresult

    def gendirectcall(self, ll_function, *args_v):
        rtyper = self.rtyper
        args_s = []
        newargs_v = []
        for v in args_v:
            if v.concretetype is Void:
                s_value = rtyper.binding(v)
                if not s_value.is_constant():
                    raise TyperError("non-constant variable of type Void")
                if not isinstance(s_value, annmodel.SomePBC):
                    # a Void non-PBC constant: can be a SomePtr pointing to a
                    # constant function.
                    assert isinstance(s_value, annmodel.SomePtr)
                    # Drop the 'const'.
                    s_value = annmodel.SomePtr(s_value.ll_ptrtype)
                    # Modify args_v so that 'v' gets the llptr concretetype
                    # stored in s_value
                    v = inputconst(s_value.ll_ptrtype, v.value)
                args_s.append(s_value)
            else:
                args_s.append(annmodel.lltype_to_annotation(v.concretetype))
            newargs_v.append(v)
        
        self.rtyper.call_all_setups()  # compute ForwardReferences now
        dontcare, spec_function = annotate_lowlevel_helper(rtyper.annotator, ll_function, args_s)

        # hack for bound methods
        if hasattr(ll_function, 'im_func'):
            newargs_v.insert(0, inputconst(Void, ll_function.im_self))

        # build the 'direct_call' operation
        f = self.rtyper.getfunctionptr(spec_function)
        c = inputconst(typeOf(f), f)
        return self.genop('direct_call', [c]+newargs_v,
                          resulttype = typeOf(f).TO.RESULT)

    def genexternalcall(self, fnname, args_v, resulttype=None, **flags):
        if isinstance(resulttype, Repr):
            resulttype = resulttype.lowleveltype
        argtypes = [v.concretetype for v in args_v]
        FUNCTYPE = FuncType(argtypes, resulttype or Void)
        f = functionptr(FUNCTYPE, fnname, **flags)
        cf = inputconst(typeOf(f), f)
        return self.genop('direct_call', [cf]+list(args_v), resulttype)

    def gencapicall(self, cfnname, args_v, resulttype=None, **flags):
        return self.genexternalcall(cfnname, args_v, resulttype=resulttype, external="C", **flags)

# _______________________________________________________________________
# this has the side-effect of registering the unary and binary operations
# and the rtyper_chooserepr() methods
from pypy.rpython import robject
from pypy.rpython import rint, rbool, rfloat
from pypy.rpython import rslice
from pypy.rpython import rlist, rstr, rtuple, rdict 
from pypy.rpython import rclass, rbuiltin, rpbc, rspecialcase
from pypy.rpython import rexternalobj
from pypy.rpython import rptr
from pypy.rpython import raddress # memory addresses
