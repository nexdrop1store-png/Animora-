# Animora eval scorecard

**Result: 17/18 passed**

## By category

| category | pass rate | passed / total |
|---|---|---|
| character | 100% | 2 / 2 |
| composition | 100% | 5 / 5 |
| furniture | 100% | 1 / 1 |
| lighting | 100% | 1 / 1 |
| primitive | 100% | 5 / 5 |
| question | 100% | 1 / 1 |
| scene | 100% | 1 / 1 |
| vehicle | 50% | 1 / 2 |

## All benchmarks

| benchmark | result | output toks | script len | issues |
|---|---|---|---|---|
| primitive.cube | PASS | 2389 | 6506 | over token budget (2389 > 1500) |
| primitive.cuboid | PASS | 1751 | 4473 | over token budget (1751 > 1500) |
| primitive.sphere | PASS | 1543 | 3986 | over token budget (1543 > 1500) |
| primitive.cylinder | PASS | 1841 | 4934 | over token budget (1841 > 1500) |
| furniture.chair.low_poly | PASS | 2517 | 6537 | — |
| vehicle.car.basic | PASS | 4958 | 13711 | — |
| vehicle.car.lambo_urus | FAIL | 6380 | 17605 | missing op `light_add\(|light\.energy\s*=` |
| character.dragon | PASS | 6799 | 19267 | — |
| scene.beach | PASS | 6456 | 18019 | — |
| lighting.studio | PASS | 3215 | 8822 | — |
| question.bsdf | PASS | 589 | 0 | — |
| primitive.sphere.creative_safety_net | PASS | 177 | 0 | — |
| composition.still_life | PASS | 6054 | 16981 | over token budget (6054 > 5000) |
| composition.depth_layering | PASS | 7454 | 20789 | — |
| composition.three_point_lighting | PASS | 3284 | 8632 | — |
| composition.rule_of_thirds | PASS | 6428 | 17982 | over token budget (6428 > 6000) |
| composition.grounded_furniture | PASS | 4874 | 13391 | — |
| character.standing_figure | PASS | 4047 | 11095 | — |

## Per-benchmark detail

### primitive.cube — PASS
- prompt: `create a cube`
- output_tokens (est): 2389
- script_length: 6506 chars
- truncated: False
- validator: ok
- named: True, material: True
- issues:
  - over token budget (2389 > 1500)

### primitive.cuboid — PASS
- prompt: `create a cuboid 2m wide, 1m tall, 0.5m deep, at origin`
- output_tokens (est): 1751
- script_length: 4473 chars
- truncated: False
- validator: ok
- named: True, material: True
- issues:
  - over token budget (1751 > 1500)

### primitive.sphere — PASS
- prompt: `add a UV sphere of radius 0.5 m at the origin`
- output_tokens (est): 1543
- script_length: 3986 chars
- truncated: False
- validator: ok
- named: True, material: True
- issues:
  - over token budget (1543 > 1500)

### primitive.cylinder — PASS
- prompt: `add a cylinder with radius 0.3m and height 1m`
- output_tokens (est): 1841
- script_length: 4934 chars
- truncated: False
- validator: ok
- named: True, material: True
- issues:
  - over token budget (1841 > 1500)

### furniture.chair.low_poly — PASS
- prompt: `Add a low-poly chair`
- output_tokens (est): 2517
- script_length: 6537 chars
- truncated: False
- validator: ok
- named: True, material: True

### vehicle.car.basic — PASS
- prompt: `Make a car`
- output_tokens (est): 4958
- script_length: 13711 chars
- truncated: False
- validator: ok
- named: True, material: True

### vehicle.car.lambo_urus — FAIL
- prompt: `Build me a hero Lamborghini Urus, studio-shot quality`
- output_tokens (est): 6380
- script_length: 17605 chars
- truncated: False
- validator: ok
- named: True, material: True
- issues:
  - missing op `light_add\(|light\.energy\s*=`

### character.dragon — PASS
- prompt: `model a low-poly dragon`
- output_tokens (est): 6799
- script_length: 19267 chars
- truncated: False
- validator: ok
- named: True, material: True

### scene.beach — PASS
- prompt: `make a beach scene with palm trees and water`
- output_tokens (est): 6456
- script_length: 18019 chars
- truncated: False
- validator: ok
- named: True, material: True

### lighting.studio — PASS
- prompt: `light this scene like a studio product shot`
- output_tokens (est): 3215
- script_length: 8822 chars
- truncated: False
- validator: ok
- named: True, material: True

### question.bsdf — PASS
- prompt: `what is a Principled BSDF node and when should I use it?`
- output_tokens (est): 589
- script_length: 0 chars
- truncated: False
- validator: ok
- named: False, material: False

### primitive.sphere.creative_safety_net — PASS
- prompt: `add something interesting to the scene`
- output_tokens (est): 177
- script_length: 0 chars
- truncated: False
- validator: ok
- named: False, material: False

### composition.still_life — PASS
- prompt: `create a still life with three objects in a triangular composition on a table`
- output_tokens (est): 6054
- script_length: 16981 chars
- truncated: False
- validator: ok
- named: True, material: True
- issues:
  - over token budget (6054 > 5000)

### composition.depth_layering — PASS
- prompt: `build an outdoor scene with a tree in the foreground, a house in the midground, and mountains in the background`
- output_tokens (est): 7454
- script_length: 20789 chars
- truncated: False
- validator: ok
- named: True, material: True

### composition.three_point_lighting — PASS
- prompt: `set up a three-point lighting rig for a portrait — key, fill, and rim lights`
- output_tokens (est): 3284
- script_length: 8632 chars
- truncated: False
- validator: ok
- named: True, material: True

### composition.rule_of_thirds — PASS
- prompt: `create a landscape scene with the horizon on the lower third of the frame and a focal subject at a rule-of-thirds intersection`
- output_tokens (est): 6428
- script_length: 17982 chars
- truncated: False
- validator: ok
- named: True, material: True
- issues:
  - over token budget (6428 > 6000)

### composition.grounded_furniture — PASS
- prompt: `create an interior room with a desk, a chair, and a lamp — every object grounded on the floor (no floating)`
- output_tokens (est): 4874
- script_length: 13391 chars
- truncated: False
- validator: ok
- named: True, material: True

### character.standing_figure — PASS
- prompt: `model a stylized human standing in T-pose with proper proportions`
- output_tokens (est): 4047
- script_length: 11095 chars
- truncated: False
- validator: ok
- named: True, material: True