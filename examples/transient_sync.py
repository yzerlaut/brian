'''
Transient synchronisation in a population of noisy IF neurons
with distance-dependent synaptic weights (organised as a ring)
'''
from brian import *

tau=10*ms
N=100
v0=5*mV
sigma=4*mV
group=NeuronGroup(N,model='dv/dt=(v0-v)/tau + sigma*xi/tau**.5 : volt',\
                  threshold=10*mV,reset=0*mV)
C=Connection(group,group,'v',structure='dense',weight=lambda i,j:.4*mV*cos(2.*pi*(i-j)*1./N)) # use a dense matrix
#f=lambda i,j:.5*mV*exp(-abs(i-j)*.1)
#C.connect_full(group,group,weight=lambda i,j:f(i,j)+f(i+N,j)+f(i,j+N))
S=SpikeMonitor(group)
R=PopulationRateMonitor(group)
group.v=rand(N)*10*mV

run(5000*ms)
subplot(211)
raster_plot(S)
subplot(223)
imshow(C.W, interpolation='nearest')
title('Synaptic connections')
subplot(224)
plot(R.times/ms,R.smooth_rate(2*ms,filter='flat'))
title('Firing rate')
show()
