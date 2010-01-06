from brian import *
from clustertools import *
from fittingparameters import *
from brian.utils.particle_swarm import *
from brian.utils.statistics import get_gamma_factor, firing_rate
try:
    import pycuda
    from gpu_modelfitting import GPUModelFitting
    can_use_gpu = True
except ImportError:
    can_use_gpu = False
import sys
    
class modelfitting_worker(object):
    def __init__(self, shared_data, use_gpu):
        """
        Initializes the object with shared data.
        What remains to be initialized is spiketimes_offset and I_offset,
        which are specific to each worker.
        """
        self.prepared = False
        
        self.total_neurons = shared_data['neurons']
        self.model = shared_data['model']
        self.threshold = shared_data['threshold']
        self.reset = shared_data['reset']
        self.input = shared_data['input']
        self.input_var = shared_data['input_var']
        self.dt = shared_data['dt']
        self.duration = shared_data['duration']
        self.total_steps = int(self.duration/self.dt)
        self.onset = shared_data['onset']
        self.stepsize = shared_data['stepsize']
        self.spiketimes = shared_data['spiketimes']
        self.initial_values = shared_data['initial_values']
        self.delta = shared_data['delta']
        self.includedelays = shared_data['includedelays']
        if shared_data['use_gpu'] is None or shared_data['use_gpu'] is True:
            self.use_gpu = use_gpu
        else:
            self.use_gpu = False
        params = shared_data['params']
        # Loads parameters
        self.fp = FittingParameters(includedelays = self.includedelays, **params)
        self.param_names = self.fp.param_names
    
    def prepare(self, (neurons, I_offset, spiketimes_offset)):
        """
        Called once the first time on each worker.
        """
        self.group = NeuronGroup(neurons, model = self.model, 
                                 reset = self.reset, threshold = self.threshold)
        if self.initial_values is not None:
            for param, value in self.initial_values.iteritems():
                self.group.state(param)[:] = value
    
        # INJECTS CURRENT
        # Injects current in consecutive subgroups, where I_offset have the same value
        # on successive intervals
        k = -1
        for i in hstack((nonzero(diff(I_offset))[0], len(I_offset)-1)):
            I_offset_subgroup_value = I_offset[i]
            I_offset_subgroup_length = i-k
            # DEBUG
#            print I_offset_subgroup_value, I_offset_subgroup_length
            sliced_subgroup = self.group.subgroup(I_offset_subgroup_length)
            input_sliced_values = self.input[I_offset_subgroup_value:I_offset_subgroup_value + self.total_steps]
            sliced_subgroup.set_var_by_array(self.input_var, TimedArray(input_sliced_values, clock=self.group.clock))
            k = i  
        
        self.I_offset = I_offset
        self.spiketimes_offset = spiketimes_offset
        
        if self.use_gpu:
            self.mf = GPUModelFitting(self.group, self.model, self.input, self.I_offset, 
                                      self.spiketimes, self.spiketimes_offset, zeros(neurons), self.delta)
        else:
            self.cc = CoincidenceCounterBis(self.group, self.spiketimes, spiketimes_offset, 
                                        onset = self.onset, delta = self.delta)
        
        self.prepared = True
        return
        
    def process(self, X):
        """
        Process job, is run separately on each worker.
        When called the first time, X contains specific data needed to initialize the group.
        When called afterwards (once per iteration), X is a matrix containing the neuron parameters
        """
        if not self.prepared:
            self.prepare(X)
            return
        # Gets the parameter values contained in the matrix X, excepted spike delays values
        if self.includedelays:
            param_values = self.fp.get_param_values(X[0:-1,:], includedelays = False)
        else:
            param_values = self.fp.get_param_values(X, includedelays = False)
        # Sets the parameter values in the NeuronGroup object
        for param, value in param_values.iteritems():
            self.group.state(param)[:] = value
            
        if self.use_gpu:
            # Reinitializes the simulation object
            self.mf.reinit_vars(self.input, self.I_offset, self.spiketimes, self.spiketimes_offset, X[-1,:])
            # LAUNCHES the simulation on the GPU
            self.mf.launch(self.duration, self.stepsize)
            # Count the final number of coincidences and of model spikes
            # by summing the numbers over all time slices
            return self.mf.coincidence_count, self.mf.spike_count
        else:
            # Sets the spike delay values
            if self.includedelays:
                self.cc.spikedelays = X[-1,:]
            # Reinitializes the simulation objects
            reinit_default_clock()
            self.cc.reinit()
            net = Network(self.group, self.cc)
            # LAUNCHES the simulation on the CPU
            net.run(self.duration)
            return self.cc.coincidences, self.cc.model_length

def modelfitting(model = None, reset = None, threshold = None, data = None, 
                 input_var = 'I', input = None, dt = None,
                 verbose = True, particles = 100, slices = 1, overlap = None,
                 iterations = 10, delta = None, initial_values = None, stepsize = 100*ms,
                 use_gpu = None, includedelays = True,
                 **params):
    
    # Use GPU ?
    if can_use_gpu & (use_gpu is not False):
        use_gpu = True
    else:
        use_gpu = False
    if use_gpu:
        slices = 1

    # Loads parameters
    fp = FittingParameters(includedelays = includedelays, **params)

    # Make sure that data is a N*2-array
    data = array(data)
    if data.ndim == 1:
        data = concatenate((zeros((len(data), 1)), data.reshape((-1,1))), axis=1)

    if dt is None:
        raise ArgumentError
    if slices == 1:
        overlap = 0*ms

    group_size = particles # Number of particles per target train
    input = input[0:slices*(len(input)/slices)] # HACK: makes sure that len(input) is a multiple of slices
    duration = len(input)*dt # duration of the input
    sliced_steps = len(input)/slices # timesteps per slice
    overlap_steps = int(overlap/dt) # timesteps during the overlap
    total_steps = sliced_steps + overlap_steps # total number of timesteps
    sliced_duration = overlap + duration/slices # duration of the vectorized simulation
    group_count = int(array(data)[:,0].max()+1) # number of target trains
    N = group_size*group_count*slices # TOTAL number of neurons

    # The neurons are first grouped by time slice : there are group_size*group_count
    #   per group/time slice
    # Within each time slice, the neurons are grouped by target train : there are
    #   group_size neurons per group/target train

    # 1. SLICES CURRENT : returns I_offset
    input = hstack((zeros(overlap_steps), input)) # add zeros at the beginning because there is no overlap from the previous slice
    I_offset = zeros(N, dtype=int)
    for slice in range(slices):
        I_offset[group_size*group_count*slice:group_size*group_count*(slice+1)] = sliced_steps*slice

    # 2. SLICES TARGET SPIKES : returns spiketimes and spiketimes_offset
    i, t = zip(*data)
    i = array(i)
    t = array(t)
    alls = []
    n = 0
    pointers = []
    
    target_length = zeros(group_count)
    target_rates = zeros(group_count)
    
    for j in range(group_count):
        s = sort(t[i==j])
        target_length[j] = len(s)
        target_rates[j] = firing_rate(s)
        for k in range(slices):
        # first sliced group : 0...0, second_train...second_train, ...
        # second sliced group : first_train_second_slice...first_train_second_slice, second_train_second_slice...
            spikeindices = (s>=k*sliced_steps*dt) & (s<(k+1)*sliced_steps*dt) # spikes targeted by sliced neuron number k, for target j
            targeted_spikes = s[spikeindices]-k*sliced_steps*dt+overlap_steps*dt # targeted spikes in the "local clock" for sliced neuron k
            targeted_spikes = hstack((-1*second, targeted_spikes, sliced_duration+1*second))
            alls.append(targeted_spikes)
            pointers.append(n)
            n += len(targeted_spikes)
    spiketimes = hstack(alls)
    pointers = array(pointers, dtype=int)
    model_target = [] # model_target[i] is the index of the first spike targetted by neuron i
    for sl in range(slices):
        for tar in range(group_count):
            model_target.append(list((sl+tar*slices)*ones(group_size)))
    model_target = array(hstack(model_target), dtype=int)
    spiketimes_offset = pointers[model_target] # [pointers[i] for i in model_target]
    spikedelays = zeros(N)

    # Duplicates each target_length value 'group_size' times so that target_length[i]
    # is the length of the train targeted by neuron i
    target_length = kron(target_length, ones(group_size))
    target_rates = kron(target_rates, ones(group_size))


    shared_data = dict(
        neurons = N, # TOTAL number of neurons
        model = model,
        threshold = threshold,
        reset = reset,
        input = input,
        input_var = input_var,
        I_offset = I_offset,
        dt = dt,
        duration = sliced_duration,
        onset = overlap,
        spiketimes = spiketimes,
        spiketimes_offset = spiketimes_offset, 
        spikedelays = spikedelays, 
        initial_values = initial_values, 
        delta = delta, 
        stepsize = stepsize, 
        includedelays = includedelays,
        use_gpu = use_gpu,
        params = params
    )
    
    if use_gpu:
        own_max_gpu = None
    else:
        own_max_gpu = 0
    manager = ClusterManager(modelfitting_worker, shared_data, own_max_gpu=own_max_gpu)
    num_processes = manager.num_processes[0]

    # Initializes the NeuronGroup objects for each worker
    N_list = [N/num_processes for _ in range(num_processes)]
    N_list[-1] = int(N-sum(N_list[:-1])) 
    
    I_offset_list = []
    spiketimes_offset_list = []
    X_list = []
    k = 0
    for i in range(num_processes):
        n = N_list[i]
        I_offset_list.append(I_offset[k:k+n])
        spiketimes_offset_list.append(spiketimes_offset[k:k+n])
        k += n
    manager.process_jobs(zip(N_list, I_offset_list, spiketimes_offset_list))

    def fun(X):
        # Repeats X once for each slice
        X = tile(X, (1, slices))
        
        X_list = []
        k = 0
        for i in range(num_processes):
            n = N_list[i]
            X_list.append(X[:,k:k+n])
            k += n
            
        results = manager.process_jobs(X_list)
        
        # Concatenates the number of coincidences and model spikes computed on each worker.
        coincidences = array([])
        model_length = array([])
        for (local_coincidences, local_model_length) in results:
            coincidences = hstack((coincidences, local_coincidences))
            model_length = hstack((model_length, local_model_length))

#        print coincidences
#        print model_length

        # Count the final number of coincidences and model spikes
        # by summing the numbers over all time slices
        coincidences = coincidences.reshape((slices,-1)).sum(axis=0)
        model_length = model_length.reshape((slices,-1)).sum(axis=0)
        # Computes the gamma factor
        gamma = get_gamma_factor(coincidences, model_length, target_length, target_rates, delta)
        return gamma
    
    initial_param_values = fp.get_initial_param_values(group_size*group_count)
    X0 = fp.get_param_matrix(initial_param_values)
    min_values, max_values = fp.set_constraints(group_size*group_count)

    X, value, T = particle_swarm(X0, fun, iterations = iterations, pso_params = [.9, 1.9, 1.9],
                     min_values = min_values, max_values = max_values,
                     group_size = group_size, verbose = verbose)
    manager.finished()
    
    best_params = fp.get_param_values(X)

    return Parameters(**best_params), value

if __name__=='__main__':
    equations = Equations('''
        dV/dt=(R*I-V)/tau : 1
        I : 1
        R : 1
        tau : second
    ''')
    
    input = loadtxt('current.txt')
    spikes = loadtxt('spikes.txt')
    print len(spikes)
    exit()
    
    params, gamma = modelfitting(model = equations, reset = 0, threshold = 1, 
                                 data = spikes, 
                                 input = input, dt = .1*ms,
                                 use_gpu = False,
                                 particles = 1, iterations = 1, delta = 2*ms,
                                 R = [1.0e9, 1.0e10], tau = [1*ms, 50*ms])
    
    print params


    
#    def get_model():
#        model = Equations('''
#            dV/dt=(R*I-V)/tau : 1
#            I : 1
#            R : 1
#            tau : second
#        ''')
#        reset = 0
#        threshold = 1
#        return model, reset, threshold
#    
#    def get_data(**params):
#        # DATA GENERATION
#        # at the end, data should be an (i,t) list
#        group = NeuronGroup(N = ntrials, model = model, reset = reset, threshold = threshold)
#        for param, value in params.iteritems():
#            group.state(param)[:] = value
#        group.I = TimedArray(input, start = 0*second, dt = defaultclock.dt)
#        
#        M = SpikeMonitor(group)
#        StM = StateMonitor(group, 'V', record = True)
#        net = Network(group, M, StM)
#        
#        reinit_default_clock()
#        net.run(duration)
#        
#        data_spikes = M.spikes
#        data_values = StM.values
#        
#        reinit_default_clock()
#        return data_spikes, data_values
#    
#    def get_current():
#        # CURRENT GENERATION
#        # at the end, I should be the list of the I values
#        # and dt the timestep
#        dt = .1*ms
#        n = int(duration/dt)
#        I = .48+.8*randn(n)
#        return I, dt
#    
#    slices = 1
#    ntrials = 1
#    duration = 500*ms
#    overlap = 0
#    group_size = 100 # number of neurons per target train
#    delta = 1*ms
#    iterations = 1
#    
#    tau0 = array([22*ms, 28*ms])
#    R0 = array([2.1, 2.5])
#    
#    model, reset, threshold = get_model()
#    input, dt = get_current()
#    data_spikes, data_values = get_data(R=R0, tau=tau0)
#    
#    i, t = zip(*data_spikes)
#    i = array(i)
#    t = array(t)
#    for j in range(ntrials):
#        s = sort(t[i==j])
#        print "Train %d" % j
#        print array(1000*s, dtype=int)/1000.0
#        print
#    
#    import time
#    start = time.clock()
#    params, gamma = modelfitting(model, reset, threshold, data_spikes, 
#                    input = input, dt = dt,
#                    verbose = True, particles = group_size, slices = slices, overlap = overlap,
#                    iterations = iterations, delta = delta, 
#                    initial_values = {'V': 0},
#                    use_gpu = False,
#                    includedelays = True,
#                    R = [2.0, 2.0, 2.6, 2.6],
#                    tau = [20*ms, 20*ms, 30*ms, 30*ms])
#    end = time.clock()
#    
#    print params
#    
#    print 'Total time: %.3f seconds' % (end-start)

