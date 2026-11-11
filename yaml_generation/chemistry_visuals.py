"""Simple schematic chemistry graphics for Manim, not quantum-accurate orbital models.

Atoms are Bohr-style illustrations; Lewis electrons are dots around a single element;
molecules are 2D ball-and-stick drawings with explicit coordinates and bonds.
"""
from __future__ import annotations
import math
from manim import VGroup, Circle, Dot, Text, Line, ORIGIN, WHITE, BLUE, YELLOW, RED, GREEN
import numpy as np

ELEMENT_COLORS = {'H': WHITE, 'C': BLUE, 'O': RED, 'N': GREEN, 'S': YELLOW}

def make_atom(spec):
    symbol = str(spec.get('symbol', spec.get('content', 'X')))
    shells = spec.get('shells', [])
    if not isinstance(shells, list) or not all(isinstance(x, int) and 0 <= x <= 32 for x in shells):
        raise ValueError('atom.shells must be a list of nonnegative integer electron counts')
    if len(shells) > 6: raise ValueError('Maximum six schematic shells')
    obj = VGroup(Circle(radius=.35, color=YELLOW), Text(symbol, font_size=24))
    for i, electrons in enumerate(shells):
        r = 0.7 + i*.43
        obj.add(Circle(radius=r, color=BLUE, stroke_opacity=.65))
        for j in range(electrons):
            theta = 2*math.pi*j/electrons + math.pi/2
            obj.add(Dot(point=np.array([r*math.cos(theta),r*math.sin(theta),0]),radius=.065,color=WHITE))
    return obj

def make_lewis(spec):
    symbol = str(spec.get('symbol',spec.get('content','X')))
    n = int(spec.get('electrons',0))
    if n < 0 or n > 8: raise ValueError('Lewis dot symbol supports 0-8 valence electrons')
    group=VGroup(Text(symbol,font_size=52))
    # Put one dot on each side first, then pair dots on those sides.
    sites=[(0,0.64),(0.68,0),(0,-0.64),(-0.68,0)]
    for i in range(n):
        x,y=sites[i%4]
        offset=(-.10 if i<4 else .10)
        if x==0: x=offset
        else: y=offset
        group.add(Dot(point=[x,y,0],radius=.07))
    charge=spec.get('charge')
    if charge is not None:
        group.add(Text(str(charge),font_size=22).move_to([.9,.9,0]))
    return group

def make_molecule(spec):
    atoms=spec.get('atoms')
    bonds=spec.get('bonds',[])
    if not isinstance(atoms,list) or not atoms:
        raise ValueError('molecule.atoms must be a nonempty list of {symbol,x,y}')
    if len(atoms)>50: raise ValueError('Maximum 50 atoms')
    xy=[]
    for a in atoms:
        if not isinstance(a,dict): raise ValueError('Each atom needs symbol,x,y')
        xy.append(np.array([float(a['x']),float(a['y']),0.]))
    group=VGroup()
    for bond in bonds:
        if not isinstance(bond,(list,tuple)) or len(bond)<2: raise ValueError('bonds must be [i,j] or [i,j,order]')
        i,j=int(bond[0]),int(bond[1]); order=int(bond[2]) if len(bond)>2 else 1
        if i<0 or j<0 or i>=len(xy) or j>=len(xy) or i==j: raise ValueError('Invalid bond indices')
        if order not in (1,2,3): raise ValueError('Only single/double/triple bonds')
        d=xy[j]-xy[i]; perp=np.array([-d[1],d[0],0]); norm=np.linalg.norm(perp)
        if norm < 1e-8: raise ValueError('Bond length cannot be zero')
        perp=perp/norm
        for k in range(order):
            shift=(k-(order-1)/2)*.11*perp
            group.add(Line(xy[i]+shift,xy[j]+shift,stroke_width=5))
    for a,pos in zip(atoms,xy):
        s=str(a['symbol']); group.add(Dot(point=pos,radius=.2,color=ELEMENT_COLORS.get(s,YELLOW)))
        group.add(Text(s,font_size=18,color=WHITE if s!='H' else BLUE).move_to(pos))
    return group
