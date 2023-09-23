from functools import partial
from itertools import combinations

import ot
import numpy as np
import scipy as sp
from scipy.stats import unitary_group

import tensorflow as tf
import tensorflow_probability as tfp
from tensorflow.python.ops.numpy_ops import np_config

import tensorcircuit as tc

import torch
import torch.nn as nn
from torch.optim.lr_scheduler import StepLR
from torch.linalg import matrix_power
from opt_einsum import contract

K = tc.set_backend('tensorflow')
tc.set_dtype('complex64')

class OneQubitDiffusionModel(nn.Module):
    def __init__(self, T, Ndata):
        '''
        the diffusion quantum circuit model to scramble arbitrary set of states to Haar random states
        Args:
        n: number of qubits
        T: number of diffusion steps
        Ndata: number of samples in the dataset
        '''
        super().__init__()
        self.t = 0
        self.T = T
        self.Ndata = Ndata
    
    def HaarSampleGeneration(self, Ndata, seed):
        '''
        generate random haar states,
        used as inputs in the t=T step for backward denoise
        Args:
        Ndata: number of samples in dataset
        '''
        np.random.seed(seed)
        states_T = unitary_group.rvs(dim=2, size=Ndata)[:,:,0]

        return tf.convert_to_tensor(states_T)
    
    def scrambleCircuit_t(self, input, phis):
        '''
        obtain the state through diffusion step t
        Args:
        t: diffusion step
        input: the input quantum state
        phis: the single-qubit rotation angles in diffusion circuit
        gs: the angle of RZZ gates in diffusion circuit when n>=2
        '''
        # input, phis = params
        c = tc.Circuit(1, inputs=input)

        for s in range(self.t):
            # single qubit rotations
            c.rz(0, theta=phis[3 * s])
            c.ry(0, theta=phis[3 * s + 1])
            c.rz(0, theta=phis[3 * s + 2])

        return c.state()
    
    def set_diffusionData_t(self, t, inputs, diff_hs, seed):
        '''
        obtain the quantum data set for 1 qubit through diffusion step t
        Args:
        t: diffusion step
        inputs: the input quantum data set
        diff_hs: the hyper-parameter to control the amplitude of quantum circuit angles
        '''
        self.t = t
        diff_hs = tf.repeat(diff_hs, 3)

        # set single-qubit rotation angles
        tf.random.set_seed(seed)
        phis = tf.random.uniform((self.Ndata, 3 * t)) * np.pi / 4. - np.pi / 8.
        phis *= diff_hs

        # states = tf.vectorized_map(partial(self.scrambleCircuit_t, t=t), (inputs, phis))
        states = K.vmap(self.scrambleCircuit_t, vectorized_argnums=(0, 1))(inputs, phis)

        return states
    
    
class MultiQubitDiffusionModel(nn.Module):
    def __init__(self, n, T, Ndata):
        '''
        the diffusion quantum circuit model to scramble arbitrary set of states to Haar random states
        Args:
        n: number of qubits
        T: number of diffusion steps
        Ndata: number of samples in the dataset
        '''
        super().__init__()
        self.n = n
        self.T = T
        self.Ndata = Ndata
    
    def HaarSampleGeneration(self, Ndata, seed):
        '''
        generate random haar states,
        used as inputs in the t=T step for backward denoise
        Args:
        Ndata: number of samples in dataset
        '''
        np.random.seed(seed)
        states_T = unitary_group.rvs(dim=2 ** self.n, size=Ndata)[:,:,0]

        return tf.convert_to_tensor(states_T)
    
    def scrambleCircuit_t(self, params, t):
        '''
        obtain the state through diffusion step t
        Args:
        t: diffusion step
        input: the input quantum state
        phis: the single-qubit rotation angles in diffusion circuit
        gs: the angle of RZZ gates in diffusion circuit when n>=2
        '''
        input, phis, gs = params
        c = tc.Circuit(self.n, inputs=input)
        for s in range(t):
            # single qubit rotations
            for i in range(self.n):
                c.rz(i, theta=phis[3 * self.n * s + i])
                c.ry(i, theta=phis[3 * self.n * s + self.n + i])
                c.rz(i, theta=phis[3 * self.n * s + 2*self.n + i])

            # homogenous RZZ on every pair of qubits (n>=2)
            for i, j in combinations(range(self.n), 2):
                c.rzz(i, j, theta=gs[s] / (2 * self.n ** 0.5))

        return c.state()
        
    def set_diffusionDataMulti_t(self, t, inputs, diff_hs, seed):
        '''
        obtain the quantum data set for multiple qubit through diffusion step t
        Args:
        t: diffusion step
        inputs: the input quantum data set
        diff_hs: the hyper-parameter to control the amplitude of quantum circuit angles
        '''
        # set single-qubit rotation angles
        tf.random.set_seed(seed)
        phis = tf.random.uniform((self.Ndata, 3 * self.n * t)) * np.pi / 4. - np.pi / 8.
        phis *= tf.repeat(diff_hs, 3 * self.n)

        # set homogenous RZZ gate angles
        gs = tf.random.uniform((self.Ndata, t)) * 0.2 + 0.4
        gs *= diff_hs
        
        states = tf.vectorized_map(partial(self.scrambleCircuit_t, t=t), (inputs, phis, gs))

        return states


def backCircuit(input, n_tot, L):
    '''
    the backward denoise parameteric quantum circuits,
    designed following the hardware-efficient ansatz
    output is the state before measurmeents on ancillas
    Args:
    input: input quantum state of n_tot qubits
    params: the parameters of the circuit
    n_tot: number of qubits in the circuits
    L: layers of circuit
    '''
    c = tc.Circuit(n_tot, inputs=input)

    for _ in range(L):
        for i in range(n_tot):
            c.rx(i, theta=0.3)
            c.ry(i, theta=0.1)

        for i in range(n_tot // 2):
            c.cz(2 * i, 2 * i + 1)

        for i in range((n_tot-1) // 2):
            c.cz(2 * i + 1, 2 * i + 2)

    return c.state()


class QDDPM_cpu(nn.Module):
    def __init__(self, n, na, T, L):
        '''
        the QDDPM model: backward process only work on cpu
        Args:
        n: number of data qubits
        na: number of ancilla qubits
        T: number of diffusion steps
        L: layers of circuit in each backward step
        '''
        super().__init__()
        self.n = n
        self.na = na
        self.n_tot = n + na
        self.T = T
        self.L = L
        # embed the circuit to a vectorized pytorch neural network layer
        self.backCircuit_vmap = K.vmap(partial(backCircuit, n_tot=self.n_tot, L=L), vectorized_argnums=0)

    def set_diffusionSet(self, states_diff):
        self.states_diff = torch.from_numpy(states_diff).cfloat()

    def randomMeasure(self, inputs):
        '''
        Given the inputs on both data & ancilla qubits before measurmenets,
        calculate the post-measurement state.
        The measurement and state output are calculated in parallel for data samples
        Args:
        inputs: states to be measured, first na qubit is ancilla
        '''
        n_batch = inputs.shape[0]
        m_probs = tf.abs(tf.reshape(inputs, [n_batch, 2 ** self.na, 2 ** self.n])) ** 2.0
        m_probs = tf.reduce_sum(m_probs, axis=2)
        m_res = tfp.distributions.Categorical(probs=m_probs).sample(1)
        indices = 2 ** self.n * tf.reshape(m_res, [-1, 1]) + tf.range(2 ** self.n)
        post_state = tf.gather(inputs, indices, batch_dims=1)
        
        return tf.linalg.normalize(post_state, axis=1)

    def backwardOutput_t(self, inputs, params):
        '''
        Backward denoise process at step t
        Args:
        inputs: the input data set at step t
        '''
        # outputs through quantum circuits before measurement
        output_full = self.backCircuit_vmap(inputs, params) 
        # perform measurement
        output_t = self.randomMeasure(output_full)

        return output_t
    
    def prepareInput_t(self, inputs_T, params_tot, t, Ndata):
        '''
        prepare the input samples for step t
        Args:
        inputs_T: the input state at the beginning of backward
        params_tot: all circuit parameters till step t+1
        '''
        self.input_tplus1 = torch.zeros((Ndata, 2**self.n_tot)).cfloat()
        self.input_tplus1[:,:2**self.n] = inputs_T
        params_tot = torch.from_numpy(params_tot).float()
        with torch.no_grad():
            for tt in range(self.T-1, t, -1):
                self.input_tplus1[:,:2**self.n] = self.backwardOutput_t(self.input_tplus1, params_tot[tt])

        return self.input_tplus1
    
    def backDataGeneration(self, inputs_T, params_tot, Ndata):
        '''
        generate the dataset in backward denoise process with training data set
        '''
        states = torch.zeros((self.T+1, Ndata, 2**self.n_tot)).cfloat()
        states[-1, :, :2**self.n] = inputs_T
        params_tot = torch.from_numpy(params_tot).float()
        with torch.no_grad():
            for tt in range(self.T-1, -1, -1):
                states[tt, :, :2**self.n] = self.backwardOutput_t(states[tt+1], params_tot[tt])

        return states


def naturalDistance(Set1, Set2):
    '''
        a natural measure on the distance between two sets of quantum states
        definition: 2*d - r1-r2
        d: mean of inter-distance between Set1 and Set2
        r1/r2: mean of intra-distance within Set1/Set2
    '''
    # a natural measure on the distance between two sets, according to trace distance
    r11 = 1. - tf.reduce_mean(tf.abs(contract('mi,ni->mn', tfm.conj(Set1), Set1))**2)
    r22 = 1. - tf.reduce_mean(tf.abs(contract('mi,ni->mn', tfm.conj(Set2), Set2))**2)
    r12 = 1. - tf.reduce_mean(tf.abs(contract('mi,ni->mn', tfm.conj(Set1), Set2))**2)
    return 2*r12 - r11 - r22


def WassDistance(Set1, Set2):
    '''
        calculate the Wasserstein distance between two sets of quantum states
        the cost matrix is the inter trace distance between sets S1, S2
    '''
    D = 1. - tf.abs(tfm.conj(Set1) @ tf.transpose(Set2))**2.
    emt = tf.constant([], dtype=tf.float32)
    Wass_dis = ot.emd2(emt, emt, M=D)
    return Wass_dis


class QDDPM(nn.Module):
    def __init__(self, n, na, T, L):
        super().__init__()
        '''
        the QDDPM model: backward process
        Args:
        n: number of data qubits
        na: number of ancilla qubits
        T: number of diffusion steps
        L: layers of circuit in each backward step
        '''
        self.n = n
        self.na = na
        self.n_tot = n + na
        self.T = T
        self.L = L
        # embed the circuit to a vectorized pytorch neural network layer
        self.qclayer = tc.TorchLayer(partial(backCircuit, n_tot=self.n_tot, L=self.L), weights_shape=[2*self.n_tot*self.L],
                                     use_vmap=True, vectorized_argnums=0)

    def set_diffusionSet(self, states_diff):
        self.states_diff = torch.from_numpy(states_diff).cfloat()

    def randomSampleGeneration(self, Ndata):
        '''
        generate random haar states,
        used as inputs in the t=T step for backward denoise
        Args:
        Ndata: number of samples in dataset
        '''
        np.random.seed(22)
        states_T = unitary_group.rvs(dim=2**self.n, size=Ndata)[:,:,0]
        return states_T

    def randomMeasure(self, input):
        '''
        Perform random meausurement on ancilla qubits in computational basis,
        return the output post-measuremenet state on data qubits.
        Currently only work on cpu
        '''
        q_idx = list(range(self.n_tot))
        c = tc.Circuit(self.n_tot, inputs=input)
        # the measurement result of ancillas
        zs, _ = c.measure_reference(*q_idx[:self.na])
        for i in range(self.na):
            c.post_select(i, keep=int(zs[i]))
            if int(zs[i]) == 1:
                c.x(i) # re-set every ancilla to be |0>
        post_state = c.state()[:2**self.n]
        normal_const = K.sqrt(K.real(post_state.conj() @ post_state))
        return post_state*(1./normal_const)
    
    def randomMeasureParallel(self, inputs):
        '''
        Given the inputs on both data & ancilla qubits before measurmenets,
        calculate the post-measurement state.
        The measurement and state output are calculated in parallel for data samples
        Currently only work for one ancilla qubit.
        Args:
        inputs: states to be measured, first qubit is ancilla
        '''
        m_probs = torch.sum(torch.abs(inputs[:,:2**self.n])**2, axis=1) # the probability of measure ancilla |0>
        m_probs = torch.vstack((m_probs, 1.-m_probs)).T
        m_res = torch.multinomial(m_probs, num_samples=1).squeeze() # measurment results
        post_state = torch.vstack((inputs[m_res==0, :2**self.n], \
                                   inputs[m_res==1, 2**self.n:])) # unnormlized post-state
        norms = torch.sqrt(torch.sum(torch.abs(post_state)**2, axis=1)).unsqueeze(dim=1)
        post_state = 1./norms * post_state # normalize the state
        return post_state

    def backwardOutput_t(self, inputs, mseq=True):
        '''
        Backward denoise process at step t
        Args:
        inputs: the input data set at step t
        mseq: Boolean variable, True/False for sequential/parallel implementation
        '''
        output_full = self.qclayer(inputs) # outputs through quantum circuits before measurement
        # perform measurement
        if mseq == True:
            output_t = []
            for i in range(inputs.shape[0]):
                output_t.append(self.randomMeasure(output_full[i]))
            output_t = torch.vstack(output_t)
        else:
            output_t = self.randomMeasureParallel(output_full)
        return output_t
    
    def prepareInput_t(self, params_tot, t, Ndata):
        '''
        prepare the input samples for step t
        Args:
        params_tot: all circuit parameters till step t+1
        '''
        self.input_tplus1 = torch.zeros((Ndata, 2**self.n_tot)).cfloat()
        self.input_tplus1[:,:2**self.n] = torch.from_numpy(self.randomSampleGeneration(Ndata)).cfloat()
        params_tot = torch.from_numpy(params_tot).float()
        with torch.no_grad():
            for tt in range(self.T-1, t, -1):
                self.qclayer.q_weights[0] = params_tot[tt] # set quantum-circuit parameters
                self.input_tplus1[:,:2**self.n] = self.backwardOutput_t(self.input_tplus1, mseq=False)
        return self.input_tplus1
