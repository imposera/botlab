"""Continuous decimal-odds threshold from the user's morning-line table.

Monotone cubic Hermite interpolation (PCHIP) preserves each supplied point.
No extrapolation or conversion from handicap-rating points is performed.
"""
from math import isfinite
POINTS=((2.4,3.5),(2.6,4.0),(2.8,4.0),(3.0,4.5),(3.5,5.0),(4.0,5.5),(4.5,6.0),(5.0,8.0))


def slopes(points):
    h=[b[0]-a[0] for a,b in zip(points,points[1:])]
    delta=[(b[1]-a[1])/step for a,b,step in zip(points,points[1:],h)]
    def edge(h0,h1,d0,d1):
        value=((2*h0+h1)*d0-h0*d1)/(h0+h1)
        if value*d0<=0:return 0.0
        if d0*d1<0 and abs(value)>3*abs(d0):return 3*d0
        return value
    result=[edge(h[0],h[1],delta[0],delta[1])]
    for i in range(1,len(points)-1):
        a,b=delta[i-1],delta[i]
        if a*b<=0:result.append(0.0)
        else:
            w1=2*h[i]+h[i-1];w2=h[i]+2*h[i-1]
            result.append((w1+w2)/(w1/a+w2/b))
    result.append(edge(h[-1],h[-2],delta[-1],delta[-2]))
    return result


SLOPES=slopes(POINTS)

def threshold(reference_odds):
    if isinstance(reference_odds,bool):return None
    try:x=float(reference_odds)
    except (ValueError,TypeError):return None
    if not isfinite(x) or not POINTS[0][0]<=x<=POINTS[-1][0]:return None
    for i,(a,b) in enumerate(zip(POINTS,POINTS[1:])):
        if x<=b[0]:
            h=b[0]-a[0];t=(x-a[0])/h
            return ((2*t**3-3*t**2+1)*a[1]+(t**3-2*t**2+t)*h*SLOPES[i]
                    +(-2*t**3+3*t**2)*b[1]+(t**3-t**2)*h*SLOPES[i+1])
