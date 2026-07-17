import urllib.request
url = 'https://dl.fbaipublicfiles.com/segment_anything/sam_vit_b_01ec64.pth'
print('Downloading SAM vit_b (375MB)...')
req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
r = urllib.request.urlopen(req)
sz = int(r.headers.get('Content-Length', 0))
print(f'Size: {sz/1024/1024:.0f}MB')
with open('weights/sam/sam_vit_b_01ec64.pth', 'wb') as f:
    f.write(r.read())
print('SAM vit_b OK')
