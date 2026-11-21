"""No imports/calls/attributes in published expression. Not an OS sandbox."""
import ast,json,resource,sys
resource.setrlimit(resource.RLIMIT_CPU,(1,1))
resource.setrlimit(resource.RLIMIT_FSIZE,(0,0))
try:
    data=json.load(sys.stdin);tree=ast.parse(data['code'],mode='eval')
    allowed=(ast.Expression,ast.Dict,ast.List,ast.Tuple,ast.Constant,ast.Name,ast.Load,ast.Compare,ast.Gt,ast.GtE,ast.Lt,ast.LtE,ast.Eq,ast.NotEq,ast.BoolOp,ast.And,ast.Or,ast.UnaryOp,ast.Not,ast.BinOp,ast.Add,ast.Sub,ast.Mult)
    if any(not isinstance(n,allowed) for n in ast.walk(tree)):raise ValueError('forbidden syntax')
    if len(data['code'])>10000:raise ValueError('expression too long')
    print(json.dumps(eval(compile(tree,'published-extension','eval'),{'__builtins__':{}},data['values'])))
except Exception as exc:
    print(type(exc).__name__+': '+str(exc),file=sys.stderr);sys.exit(2)
