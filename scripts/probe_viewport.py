import bpy
theme = bpy.context.preferences.themes[0]
vp = theme.view_3d

def c2h(c):
    try: return '#{:02X}{:02X}{:02X}'.format(int(c[0]*255),int(c[1]*255),int(c[2]*255))
    except: return str(c)

print("=== view_3d direct attrs ===")
for a in dir(vp):
    if a.startswith('_'): continue
    v = getattr(vp, a, None)
    try:
        if hasattr(v,'__len__') and len(v) in (3,4): print(f"  vp.{a} = {c2h(v)}")
        elif isinstance(v,(int,float,bool,str)): print(f"  vp.{a} = {v}")
    except: pass

sp = getattr(vp,'space',None)
if sp:
    print("\n=== view_3d.space attrs ===")
    for a in dir(sp):
        if a.startswith('_'): continue
        v = getattr(sp, a, None)
        try:
            if hasattr(v,'__len__') and len(v) in (3,4): print(f"  sp.{a} = {c2h(v)}")
            elif isinstance(v,(int,float,bool,str)): print(f"  sp.{a} = {v}")
        except: pass
