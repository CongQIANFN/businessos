import sys,time,os
from runtime import Runtime
rt=Runtime(sys.argv[1]);token=sys.argv[2]
if '--once' in sys.argv:print(rt.tick(token))
else:
    while True:
        rt.tick(token)
        if os.getenv('EXPERIMENT_SUPPLIER_URL'):rt.deliver_pending(token,os.environ['EXPERIMENT_SUPPLIER_URL'])
        time.sleep(.2)
