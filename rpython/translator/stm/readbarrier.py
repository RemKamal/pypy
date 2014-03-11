from rpython.flowspace.model import SpaceOperation
from rpython.translator.unsimplify import varoftype
from rpython.rtyper.lltypesystem import lltype


READ_OPS = set(['getfield', 'getarrayitem', 'getinteriorfield', 'raw_load'])


def is_gc_ptr(T):
    return isinstance(T, lltype.Ptr) and T.TO._gckind == 'gc'


def insert_stm_read_barrier(transformer, graph):
    # We need to put enough 'stm_read' in the graph so that any
    # execution of a READ_OP on some GC object is guaranteed to also
    # execute either 'stm_read' or 'stm_write' on the same GC object
    # during the same transaction.
    #
    # XXX this can be optimized a lot, but for now we go with the
    # simplest possible solution...
    #
    for block in graph.iterblocks():
        if not block.operations:
            continue
        newops = []
        for op in block.operations:
            if op.opname in READ_OPS and is_gc_ptr(op.args[0].concretetype):
                v_none = varoftype(lltype.Void)
                newops.append(SpaceOperation('stm_read', [op.args[0]], v_none))
            newops.append(op)
        block.operations = newops
