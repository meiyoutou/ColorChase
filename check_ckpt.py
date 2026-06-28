import torch

ckpt = torch.load(r'D:\桌面\best.ckpt', map_location='cpu', weights_only=False)
sd = ckpt['state_dict']

prefixes = set()
for k in sd.keys():
    parts = k.split('.')
    p = '.'.join(parts[:2])
    prefixes.add(p)

for p in sorted(prefixes):
    count = sum(1 for k in sd if k.startswith(p))
    print(f'{p}: {count} params')

print()
for pattern in ['norm_stage', 'style_stage', 'norm', 'style', 'encoder', 'dncm', 'mapping']:
    matching = [k for k in sd if pattern in k.lower()]
    if matching:
        print(f'Keys matching "{pattern}": {len(matching)}')
        for k in matching[:5]:
            print(f'  {k}')