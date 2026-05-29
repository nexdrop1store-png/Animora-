# Animora eval scorecard

**Result: 8/12 passed**

## By category

| category | pass rate | passed / total |
|---|---|---|
| character | 100% | 1 / 1 |
| furniture | 0% | 0 / 1 |
| lighting | 100% | 1 / 1 |
| primitive | 40% | 2 / 5 |
| question | 100% | 1 / 1 |
| scene | 100% | 1 / 1 |
| vehicle | 100% | 2 / 2 |

## All benchmarks

| benchmark | result | output toks | script len | issues |
|---|---|---|---|---|
| primitive.cube | FAIL | 714 | 1693 | no meaningful .name= assignment |
| primitive.cuboid | PASS | 706 | 1533 | — |
| primitive.sphere | FAIL | 392 | 735 | no meaningful .name= assignment |
| primitive.cylinder | FAIL | 657 | 1390 | no meaningful .name= assignment |
| furniture.chair.low_poly | FAIL | 1123 | 2690 | no meaningful .name= assignment |
| vehicle.car.basic | PASS | 248 | 0 | — |
| vehicle.car.lambo_urus | PASS | 0 | 0 | — |
| character.dragon | PASS | 6709 | 19145 | — |
| scene.beach | PASS | 8824 | 25275 | — |
| lighting.studio | PASS | 3841 | 10430 | over token budget (3841 > 3500) |
| question.bsdf | PASS | 716 | 0 | — |
| primitive.sphere.creative_safety_net | PASS | 3566 | 9791 | — |

## Per-benchmark detail

### primitive.cube — FAIL
- prompt: `create a cube`
- output_tokens (est): 714
- script_length: 1693 chars
- truncated: False
- validator: ok
- named: False, material: True
- issues:
  - no meaningful .name= assignment

### primitive.cuboid — PASS
- prompt: `create a cuboid 2m wide, 1m tall, 0.5m deep, at origin`
- output_tokens (est): 706
- script_length: 1533 chars
- truncated: False
- validator: ok
- named: True, material: True

### primitive.sphere — FAIL
- prompt: `add a UV sphere of radius 0.5 m at the origin`
- output_tokens (est): 392
- script_length: 735 chars
- truncated: False
- validator: ok
- named: False, material: True
- issues:
  - no meaningful .name= assignment

### primitive.cylinder — FAIL
- prompt: `add a cylinder with radius 0.3m and height 1m`
- output_tokens (est): 657
- script_length: 1390 chars
- truncated: False
- validator: ok
- named: False, material: True
- issues:
  - no meaningful .name= assignment

### furniture.chair.low_poly — FAIL
- prompt: `Add a low-poly chair`
- output_tokens (est): 1123
- script_length: 2690 chars
- truncated: False
- validator: ok
- named: False, material: True
- issues:
  - no meaningful .name= assignment

### vehicle.car.basic — PASS
- prompt: `Make a car`
- output_tokens (est): 248
- script_length: 0 chars
- truncated: False
- validator: ok
- named: False, material: False

### vehicle.car.lambo_urus — PASS
- prompt: `Build me a hero Lamborghini Urus, studio-shot quality`
- output_tokens (est): 0
- script_length: 0 chars
- truncated: False
- validator: ok
- named: False, material: False

### character.dragon — PASS
- prompt: `model a low-poly dragon`
- output_tokens (est): 6709
- script_length: 19145 chars
- truncated: False
- validator: ok
- named: True, material: True

### scene.beach — PASS
- prompt: `make a beach scene with palm trees and water`
- output_tokens (est): 8824
- script_length: 25275 chars
- truncated: False
- validator: ok
- named: True, material: True

### lighting.studio — PASS
- prompt: `light this scene like a studio product shot`
- output_tokens (est): 3841
- script_length: 10430 chars
- truncated: False
- validator: ok
- named: True, material: True
- issues:
  - over token budget (3841 > 3500)

### question.bsdf — PASS
- prompt: `what is a Principled BSDF node and when should I use it?`
- output_tokens (est): 716
- script_length: 0 chars
- truncated: False
- validator: ok
- named: False, material: False

### primitive.sphere.creative_safety_net — PASS
- prompt: `add something interesting to the scene`
- output_tokens (est): 3566
- script_length: 9791 chars
- truncated: False
- validator: ok
- named: True, material: True