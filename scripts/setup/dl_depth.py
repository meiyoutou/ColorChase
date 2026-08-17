import os, urllib.request, sys
os.makedirs('weights/depth_anything_v2', exist_ok=True)
url = 'https://huggingface.co/depth-anything/Depth-Anything-V2-Base/resolve/main/depth_anything_v2_vitb.pth'
local = 'weights/depth_anything_v2/depth_anything_v2_vitb.pth'
if os.path.exists(local) and os.path.getsize(local) > 10:
    print(f'Skipping, already exists: {os.path.getsize(local)/1024/1024:.0f}MB')
    sys.exit(0)
print('Downloading Depth Anything V2 Base...')
req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
r = urllib.request.urlopen(req)
sz = int(r.headers.get('Content-Length', 0))
print(f'Size: {sz/1024/1024:.0f}MB')
with open(local, 'wb') as f:
    f.write(r.read())
print('Depth base OK')
