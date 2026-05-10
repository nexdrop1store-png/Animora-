"""Probe current theme state to find grey sources."""
import bpy

def c2h(c):
    try:
        if len(c) >= 3:
            return '#{:02X}{:02X}{:02X}'.format(int(c[0]*255), int(c[1]*255), int(c[2]*255))
    except:
        return str(c)
    return str(c)

def dump_wcol(name, w):
    if w is None:
        return
    attrs = ['inner','inner_sel','outline','outline_sel','item','text','text_sel','roundness','shaded']
    vals = {}
    for a in attrs:
        v = getattr(w, a, None)
        if v is None: continue
        try:
            vals[a] = c2h(v) if hasattr(v,'__len__') else v
        except: vals[a] = v
    print(f"  {name}: inner={vals.get('inner','?')} inner_sel={vals.get('inner_sel','?')} "
          f"outline={vals.get('outline','?')} roundness={vals.get('roundness','?')} shaded={vals.get('shaded','?')}")

theme = bpy.context.preferences.themes[0]
ui = theme.user_interface

print("=== WIDGET COLOURS ===")
for name in ['wcol_regular','wcol_tool','wcol_num','wcol_numslider','wcol_menu',
             'wcol_menu_back','wcol_menu_item','wcol_list_item','wcol_box','wcol_scroll','wcol_tab']:
    dump_wcol(name, getattr(ui, name, None))

print("\n=== SPACE BACKS ===")
spaces = ['view_3d','properties','outliner','topbar','statusbar','preferences']
for sp_name in spaces:
    sp = getattr(theme, sp_name, None)
    if sp is None: continue
    for target in (getattr(sp,'space',None), sp):
        if target is None: continue
        back = getattr(target,'back',None)
        hdr  = getattr(target,'header',None)
        btn  = getattr(target,'button',None)
        if back is not None:
            print(f"  {sp_name}: back={c2h(back)}  header={c2h(hdr)}  button={c2h(btn) if btn else 'n/a'}")
        break

print("\n=== VIEWPORT ===")
vp = theme.view_3d
sp = getattr(vp,'space',None) or vp
for attr in ['back','gradstart','gradend','header','button']:
    v = getattr(sp, attr, None)
    if v is not None:
        print(f"  view_3d.space.{attr} = {c2h(v)}")

print("\n=== SYSTEM ===")
sys = bpy.context.preferences.system
print(f"  ui_scale={sys.ui_scale}  widget_unit={sys.widget_unit}  dpi={sys.dpi}")

print("\n=== ICON ===")
for attr in ['icon_alpha','icon_saturation']:
    v = getattr(ui, attr, None)
    if v is not None: print(f"  {attr}={v}")
for attr in ['icon_scene','icon_object','icon_modifier']:
    v = getattr(ui, attr, None)
    if v is not None: print(f"  {attr}={c2h(v)}")
