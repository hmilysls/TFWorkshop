import tensorflow as tf
from nick_tf import dynamic_attention_wrapper
from tensorflow.contrib.rnn import  MultiRNNCell, AttentionCellWrapper, GRUCell, LSTMCell, LSTMStateTuple
from tensorflow.python.ops.rnn import dynamic_rnn, bidirectional_dynamic_rnn
from tensorflow.python.layers import core as layers_core

DynamicAttentionWrapper = dynamic_attention_wrapper.DynamicAttentionWrapper
DynamicAttentionWrapperState = dynamic_attention_wrapper.DynamicAttentionWrapperState 
Bahdanau = dynamic_attention_wrapper.BahdanauAttention
Luong = dynamic_attention_wrapper.LuongAttention

import logging as log
graphlg = log.getLogger("graph")

# Dynamic RNN creater for specific cell_model, num_units, num_layers, etc
def DynRNN(cell_model, num_units, num_layers, emb_inps, enc_lens, keep_prob=1.0, bidi=False, name_scope="encoder", dtype=tf.float32):
	"""A Dynamic RNN Creator"
		Take embedding inputs and make dynamic rnn process 
	"""
	with tf.name_scope(name_scope) as scope:
		if bidi:
			cell_fw = CreateMultiRNNCell(cell_model, num_units, num_layers, keep_prob, name_scope="cell_fw")
			cell_bw = CreateMultiRNNCell(cell_model, num_units, num_layers, keep_prob, name_scope="cell_bw")
			enc_outs, enc_states = bidirectional_dynamic_rnn(cell_fw=cell_fw, cell_bw=cell_bw,
															inputs=emb_inps,
															sequence_length=enc_lens,
															dtype=dtype,
															parallel_iterations=16,
															scope=name_scope)
			fw_s, bw_s = enc_states 
			enc_states = []
			for f, b in zip(fw_s, bw_s):
				if isinstance(f, LSTMStateTuple):
					enc_states.append(LSTMStateTuple(tf.concat([f.c, b.c], axis=1), tf.concat([f.h, b.h], axis=1)))
				else:
					enc_states.append(tf.concat([f, b], 1))

			enc_outs = tf.concat([enc_outs[0], enc_outs[1]], axis=2)
			mem_size = 2 * num_units
			enc_state_size = 2 * num_units 
		else:
			cell = CreateMultiRNNCell(cell_model, num_units, num_layers, keep_prob, name_scope="cell")
			enc_outs, enc_states = dynamic_rnn(cell=cell,
											   inputs=emb_inps,
											   sequence_length=enc_lens,
											   parallel_iterations=16,
											   dtype=dtype,
											   scope=name_scope)
			mem_size = num_units
			enc_state_size = num_units
	return enc_outs, enc_states, mem_size, enc_state_size

def CreateMultiRNNCell(cell_name, num_units, num_layers=1, output_keep_prob=1.0, reuse=False, name_scope=None):
	"""Create a multi rnn cell object
		create multi layer cells with specific size, layers and drop prob
	"""
	with tf.variable_scope(name_scope):
		cells = []
		for i in range(num_layers):
			if cell_name == "GRUCell":
				single_cell = GRUCell(num_units=num_units, reuse=reuse)
			elif cell_name == "LSTMCell":
				single_cell = LSTMCell(num_units=num_units, reuse=reuse)
			else:
				graphlg.info("Unknown Cell type !")
				exit(0)
			if output_keep_prob < 1.0:
				single_cell = tf.contrib.rnn.DropoutWrapper(single_cell, output_keep_prob=output_keep_prob) 
				graphlg.info("Layer %d, Dropout used: output_keep_prob %f" % (i, output_keep_prob))
			cells.append(single_cell)
	return MultiRNNCell(cells)

def AttnCell(cell_model, num_units, num_layers, memory, mem_lens, attn_type, max_mem_size, keep_prob=1.0, addmem=False, dtype=tf.float32, name_scope="attn_cell"):
	# Attention  
	"""Wrap a cell by specific attention mechanism with some memory
	Params:
		max_mem_size is for incremental enc memory (for addmem attention)
	"""
	with tf.name_scope(name_scope):
		decoder_cell = CreateMultiRNNCell(cell_model, num_units, num_layers, keep_prob, False, name_scope)
		if attn_type == "Luo":
			mechanism = dynamic_attention_wrapper.LuongAttention(num_units=num_units, memory=memory,
																	max_mem_size=max_mem_size,
																	memory_sequence_length=mem_lens)
		elif attn_type == "Bah":
			mechanism = dynamic_attention_wrapper.BahdanauAttention(num_units=num_units, memory=memory, 
																	max_mem_size=max_mem_size,
																	memory_sequence_length=mem_lens)
		elif attn_type == None:
			return decoder_cell
		else:
			print "Unknown attention stype, must be Luo or Bah" 
			exit(0)
		attn_cell = DynamicAttentionWrapper(cell=decoder_cell, attention_mechanism=mechanism,
												attention_size=num_units, addmem=addmem)
		return attn_cell

def DecStateInit(all_enc_states, decoder_cell, batch_size, init_type="each2each", use_proj=True):
	"""make init states for decoder cells
		take some states (maybe for each encoder layers) to make different
		type of init states for decoder cells
	"""
	# Encoder states for initial state, with vae 
	with tf.name_scope("DecStateInit"):
		# get decoder zero_states as a default and shape guide
		zero_states = decoder_cell.zero_state(dtype=tf.float32, batch_size=batch_size)
		if isinstance(zero_states, DynamicAttentionWrapperState):
			dec_zero_states = zero_states.cell_state
		else:
			dec_zero_states = zero_states
		
		#TODO check all_enc_states

		init_states = []
		if init_type == "each2each":
			for i, each in enumerate(dec_zero_states):
				if i >= len(all_enc_states):	
					init_states.append(each)
					continue
				if use_proj == False:
					init_states.append(all_enc_states[i])
					continue
				enc_state = all_enc_states[i]
				if isinstance(each, LSTMStateTuple):
					init_h = tf.layers.dense(enc_state.h, each.h.get_shape()[1], name="proj_l%d_to_h" % i)
					init_c = tf.layers.dense(enc_state.c, each.c.get_shape()[1], name="proj_l%d_to_c" % i)
					init_states.append(LSTMStateTuple(init_c, init_h))
				else:
					init = tf.layers.dense(enc_state, each.get_shape()[1], name="ToDecShape")
					init_states.append(init)
		elif init_type == "all2first":
			enc_state = tf.concat(all_enc_states, 1)
			dec_state = dec_zero_states[0]
			if isinstance(dec_state, LSTMStateTuple):
				init_h = tf.layers.dense(enc_state, dec_state.h.get_shape()[1], name="ToDecShape")
				init_c = tf.layers.dense(enc_state, dec_state.c.get_shape()[1], name="ToDecShape")
				init_states.append(LSTMStateTuple(init_c, init_h))
			else:
				init = tf.layers.dense(enc_state, dec_state.get_shape()[1], name="ToDecShape")
				init_states.append(init)
			init_states.extend(dec_zero_states[1:])
		elif init_type == "allzeros":
			init_states.dec_zero_states
		else:	
			print "init type %s unknonw !!!" % init_type
			exit(0)

		if isinstance(decoder_cell,DynamicAttentionWrapper):
			zero_states = DynamicAttentionWrapperState(tuple(init_states), zero_states.attention, zero_states.newmem, zero_states.alignments)
		else:
			zero_states = tuple(init_states)
		
		return zero_states

def CreateVAE(states, enc_latent_dim, mu_prior=None, logvar_prior=None, reuse=False, dtype=tf.float32, name_scope=None):
	"""Create vae states and kld with specific distribution
		encode all input states into a random variable, and create a KLD
		between prior and latent isolated Gaussian distribution
	"""
	with tf.name_scope(name_scope) as scope:
		graphlg.info("Creating latent z for encoder states") 
		all_states = []
		for each in states:
			all_states.extend(list(each))
		h_state = tf.concat(all_states, 1, name="concat_states")
		epsilon = tf.random_normal([tf.shape(h_state)[0], enc_latent_dim])
		with tf.name_scope("EncToLatent"):
			W_enc_hidden_mu = tf.Variable(tf.random_normal([int(h_state.get_shape()[1]), enc_latent_dim]),name="w_enc_hidden_mu")
			b_enc_hidden_mu = tf.Variable(tf.random_normal([enc_latent_dim]), name="b_enc_hidden_mu") 
			W_enc_hidden_logvar = tf.Variable(tf.random_normal([int(h_state.get_shape()[1]), enc_latent_dim]), name="w_enc_hidden_logvar")
			b_enc_hidden_logvar = tf.Variable(tf.random_normal([enc_latent_dim]), name="b_enc_hidden_logvar") 
			# Should there be any non-linearty?
			# A normal sampler
			mu_enc = tf.tanh(tf.matmul(h_state, W_enc_hidden_mu) + b_enc_hidden_mu)
			logvar_enc = tf.matmul(h_state, W_enc_hidden_logvar) + b_enc_hidden_logvar
			z = mu_enc + tf.exp(0.5 * logvar_enc) * epsilon

		if mu_prior == None:
			mu_prior = tf.zeros_like(epsilon)
		if logvar_prior == None:
			logvar_prior = tf.zeros_like(epsilon)

		# Should this z be concatenated by original state ?
		with tf.name_scope("KLD"):
			KLD = -0.5 * tf.reduce_sum(1 + logvar_enc - logvar_prior - (tf.pow(mu_enc - mu_prior, 2) + tf.exp(logvar_enc))/tf.exp(logvar_prior), axis = 1)
	return z, KLD, None 