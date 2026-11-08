import unittest
from PIL import Image
from centre_interval import patches
class Geometry(unittest.TestCase):
 def test_all_geometry(self):
  for w in [3,4,10,29,128,246,297]:
   for h in [1,2,3,4,7,8,23,24,127,128,129,383,384,385,1000]:
    im=Image.new('RGB',(w,h),'white');ims,bs=patches(lambda b:im.crop(b),[0,0,w,h],dict(top_y=0,bottom_y=h))
    pad=min(4,h//8)
    self.assertEqual(bs[0][1],pad)
    self.assertEqual(bs[-1][3],h-pad)
    for p,b in zip(ims,bs):
     self.assertTrue(0<=b[0]<b[2]<=w and 0<=b[1]<b[3]<=h)
     self.assertLessEqual(abs((b[0]+b[2])/2-w/2),.5)
     self.assertEqual(p.size,(b[2]-b[0],b[3]-b[1]))
     self.assertEqual(p.height,min(32,h-2*pad))
if __name__=='__main__':unittest.main()
