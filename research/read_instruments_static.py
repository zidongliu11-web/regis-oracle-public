"""Parse primitive protocol-2 metadata without importing or executing pickle globals.

GLOBAL/REDUCE/NEWOBJ/BUILD are inert nodes. Only literal date encodings are decoded.
Persistent IDs, extensions, opaque objects used as keys, cycles, and other protocols fail.
"""
from dataclasses import dataclass
from datetime import date
import pickletools

@dataclass(frozen=True)
class Inert:
    kind: str
    value: object

def parse(data, max_ops=4000000, max_memo=500000, checkpoint=None):
    if len(data)>32*1024**2:raise ValueError('instrument bytes ceiling')
    stack=[];memo={};mark=object();counts={};stopped=False;ops=0
    def marked():
        i=len(stack)-1
        while i>=0 and stack[i] is not mark:i-=1
        if i<0:raise ValueError('missing mark')
        values=stack[i+1:];del stack[i:];return values
    def pairs(values):
        if len(values)%2:raise ValueError('odd dictionary fields')
        result={}
        for k,v in zip(values[::2],values[1::2]):
            if not isinstance(k,(str,int,float,bytes,bool,type(None))):raise ValueError('nonliteral dictionary key')
            if k in result:raise ValueError('duplicate dictionary key')
            result[k]=v
        return result
    for op,arg,pos in pickletools.genops(data):
        ops+=1;name=op.name;counts[name]=counts.get(name,0)+1
        if checkpoint is not None and ops%8192==0:checkpoint()
        if ops>max_ops or len(stack)>200000:raise ValueError('instrument operations/stack ceiling')
        if name=='PROTO':
            if pos!=0 or arg!=2:raise ValueError('only protocol2 supported')
        elif name=='MARK':stack.append(mark)
        elif name in ('BININT','BININT1','BININT2','INT','LONG','LONG1','LONG4','FLOAT','BINFLOAT','BINUNICODE','UNICODE','BINSTRING','SHORT_BINSTRING','STRING'):stack.append(arg)
        elif name=='NONE':stack.append(None)
        elif name=='NEWTRUE':stack.append(True)
        elif name=='NEWFALSE':stack.append(False)
        elif name=='EMPTY_LIST':stack.append([])
        elif name=='EMPTY_DICT':stack.append({})
        elif name=='EMPTY_TUPLE':stack.append(())
        elif name=='LIST':stack.append(marked())
        elif name=='TUPLE':stack.append(tuple(marked()))
        elif name in ('TUPLE1','TUPLE2','TUPLE3'):
            n=int(name[-1]);v=stack[-n:];del stack[-n:];stack.append(tuple(v))
        elif name=='DICT':stack.append(pairs(marked()))
        elif name in ('APPEND','APPENDS'):
            v=[stack.pop()] if name=='APPEND' else marked()
            if not stack or not isinstance(stack[-1],list):raise ValueError('append target')
            if any(x is stack[-1] for x in v):raise ValueError('direct list cycle')
            stack[-1].extend(v)
        elif name in ('SETITEM','SETITEMS'):
            v=[stack.pop(),stack.pop()][::-1] if name=='SETITEM' else marked();d=pairs(v)
            if not stack or not isinstance(stack[-1],dict):raise ValueError('setitem target')
            if set(d)&set(stack[-1]):raise ValueError('duplicate dictionary field')
            if any(x is stack[-1] for x in d.values()):raise ValueError('direct dictionary cycle')
            stack[-1].update(d)
        elif name in ('BINPUT','LONG_BINPUT','PUT'):
            i=int(arg)
            if i<0 or i>=max_memo or i in memo or not stack:raise ValueError('memo ceiling/duplicate')
            memo[i]=stack[-1]
        elif name in ('BINGET','LONG_BINGET','GET'):stack.append(memo[int(arg)])
        elif name=='GLOBAL':stack.append(Inert('global',arg))
        elif name in ('REDUCE','NEWOBJ'):
            args=stack.pop();symbol=stack.pop();stack.append(Inert(name.lower(),(symbol,args)))
        elif name=='BUILD':
            state=stack.pop();obj=stack.pop();stack.append(Inert('build',(obj,state)))
        elif name=='POP':stack.pop()
        elif name=='POP_MARK':marked()
        elif name=='DUP':stack.append(stack[-1])
        elif name=='STOP':
            if pos!=len(data)-1 or len(stack)!=1:raise ValueError('trailing data/invalid stack')
            stopped=True;break
        else:raise ValueError('unsupported inert opcode '+name)
    if not stopped or not isinstance(stack[0],list) or not all(isinstance(x,dict) for x in stack[0]):raise ValueError('expected list of literal dictionaries')
    reject_cycles(stack[0],checkpoint=checkpoint)
    return stack[0],counts

def reject_cycles(root,max_nodes=1000000,max_depth=128,checkpoint=None):
    """Iterative DFS: shared acyclic containers allowed; back edges rejected."""
    def children(v):
        if isinstance(v,dict):return iter(v.values())
        if isinstance(v,(list,tuple)):return iter(v)
        if isinstance(v,Inert):return iter((v.value,))
        return None
    colors={id(root):1};pending=[(root,children(root))]
    while pending:
        if checkpoint is not None:checkpoint()
        obj,iterator=pending[-1]
        try:child=next(iterator)
        except StopIteration:
            colors[id(obj)]=2;pending.pop();continue
        nested=children(child)
        if nested is None:continue
        marker=colors.get(id(child),0)
        if marker==1:raise ValueError('indirect metadata cycle')
        if marker==2:continue
        if len(colors)>=max_nodes or len(pending)>=max_depth:raise ValueError('metadata graph/depth ceiling')
        colors[id(child)]=1;pending.append((child,nested))

def date_int(value):
    if value is None or value in ('','0000-00-00','2999-12-31'):return None
    if isinstance(value,str):
        try:
            result=int(date.fromisoformat(value[:10]).strftime('%Y%m%d'))
            return None if result==29991231 else result
        except (ValueError,TypeError):return None
    if isinstance(value,int) and 19000101<=value<=29991231:
        s=str(value)
        try:date(int(s[:4]),int(s[4:6]),int(s[6:8]));return None if value==29991231 else value
        except ValueError:return None
    # Python protocol2 datetime uses an inert _codecs.encode(...,'latin1') literal.
    if isinstance(value,Inert) and value.kind=='reduce':
        symbol,args=value.value
        if symbol==Inert('global','datetime date') and isinstance(args,tuple) and len(args)==1:
            payload=args[0]
            if isinstance(payload,Inert) and payload.kind=='reduce':
                encoder,literals=payload.value
                if encoder==Inert('global','_codecs encode') and isinstance(literals,tuple) and len(literals)==2 and literals[1]=='latin1' and isinstance(literals[0],str):payload=literals[0].encode('latin1')
            if isinstance(payload,(bytes,str)):
                b=payload.encode('latin1') if isinstance(payload,str) else payload
                if len(b)==4:
                    try:
                        result=int(date(b[0]*256+b[1],b[2],b[3]).strftime('%Y%m%d'))
                        return None if result==29991231 else result
                    except ValueError:return None
    return None
