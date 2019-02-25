""" Experiment runner for the model with knowledge graph attached to interaction data """

# python train.py -d douban --accum stackRGGCN -do 0.7 -nleft -nb 2 -e 200 --hidden 100 75 --num_layers 5

from __future__ import division
from __future__ import print_function

import argparse
import datetime
import time

import tensorflow as tf
import numpy as np
import scipy.sparse as sp
import sys
import json

from preprocessing import create_trainvaltest_split, \
	sparse_to_tuple, preprocess_user_item_features, globally_normalize_bipartite_adjacency, \
	load_data_monti, load_official_trainvaltest_split, normalize_features, get_edges_matrices, load_facebook_data, load_facebook_data_link_prediction
from model import RecommenderGAE, RecommenderSideInfoGAE
from utils import construct_feed_dict

# Set random seed
# seed = 123 # use only for unit testing
seed = int(time.time())
np.random.seed(seed)
tf.set_random_seed(seed)

# Settings
ap = argparse.ArgumentParser()
ap.add_argument("-d", "--dataset", type=str, default="ml_1m",
				choices=['ml_100k', 'ml_1m', 'ml_10m', 'douban', 'yahoo_music', 'flixster', 'facebook'],
				help="Dataset string.")

ap.add_argument("-lr", "--learning_rate", type=float, default=0.01,
				help="Learning rate")

ap.add_argument("-e", "--epochs", type=int, default=2500,
				help="Number training epochs")

ap.add_argument("-hi", "--hidden", type=int, nargs=2, default=[500, 75],
				help="Number hidden units in 1st and 2nd layer")

ap.add_argument("-numlay", "--num_layers", type=int, default=1,
				help="Number of graph conv layers")

ap.add_argument("-cons", "--consecutive", type=int, default=2,
				help="Number of consecutive failures to improve before decaying learning rate")

ap.add_argument("-dr", "--decay_rate", type=float, default=1.25,
				help="Decay rate of learning rate")

ap.add_argument("-fhi", "--feat_hidden", type=int, default=64,
				help="Number hidden units in the dense layer for features")

ap.add_argument("-ac", "--accumulation", type=str, default="sum", choices=['sum', 'stack', 'stackRGGCN', 'sumRGGCN', 'stackSimple', 'simple', 'stackGCNGate'],
				help="Accumulation function: sum or stack or stackRGGCN or sumRGGCN.")

ap.add_argument("-do", "--dropout", type=float, default=0.7,
				help="Dropout fraction")

ap.add_argument("-nb", "--num_basis_functions", type=int, default=2,
				help="Number of basis functions for Mixture Model GCN.")

ap.add_argument("-ds", "--data_seed", type=int, default=1234,
				help="""Seed used to shuffle data in data_utils, taken from cf-nade (1234, 2341, 3412, 4123, 1324).
					 Only used for ml_1m and ml_10m datasets. """)

ap.add_argument("-sdir", "--summaries_dir", type=str, default='logs/' + str(datetime.datetime.now()).replace(' ', '_'),
				help="Directory for saving tensorflow summaries.")

# Boolean flags
fp = ap.add_mutually_exclusive_group(required=False)
fp.add_argument('-nsym', '--norm_symmetric', dest='norm_symmetric',
				help="Option to turn on symmetric global normalization", action='store_true')
fp.add_argument('-nleft', '--norm_left', dest='norm_symmetric',
				help="Option to turn on left global normalization", action='store_false')
ap.set_defaults(norm_symmetric=True)

fp = ap.add_mutually_exclusive_group(required=False)
fp.add_argument('-f', '--features', dest='features',
				help="Whether to use features (1) or not (0)", action='store_true')
fp.add_argument('-no_f', '--no_features', dest='features',
				help="Whether to use features (1) or not (0)", action='store_false')
ap.set_defaults(features=False)

fp = ap.add_mutually_exclusive_group(required=False)
fp.add_argument('-ws', '--write_summary', dest='write_summary',
				help="Option to turn on summary writing", action='store_true')
fp.add_argument('-no_ws', '--no_write_summary', dest='write_summary',
				help="Option to turn off summary writing", action='store_false')
ap.set_defaults(write_summary=False)

fp = ap.add_mutually_exclusive_group(required=False)
fp.add_argument('-t', '--testing', dest='testing',
				help="Option to turn on test set evaluation", action='store_true')
fp.add_argument('-v', '--validation', dest='testing',
				help="Option to only use validation set evaluation", action='store_false')
ap.set_defaults(testing=False)

ap.add_argument('-gi', '--use_gcmc_indices', action='store_true', help='Option to use original GCMC way of producing user/item indices')

ap.add_argument('-de', '--dropout_edges', action='store_true', help='Option to do dropout on edges too')

ap.add_argument('-ne', '--normalise_edges', action='store_true', help='Option to normalise edges')

ap.add_argument("-of", "--observed_fraction", type=float, default=0.8, help="Observed fraction of users, for facebook dataset")

ap.add_argument("-vf", "--val_fraction", type=float, default=0.2, help="Validation fraction, for facebook dataset")
ap.add_argument("-tf", "--test_fraction", type=float, default=0.2, help="Test fraction, for facebook dataset")


args = vars(ap.parse_args())

print('Settings:')
print(args, '\n')

# Define parameters
DATASET = args['dataset']
DATASEED = args['data_seed']
NB_EPOCH = args['epochs']
DO = args['dropout']
HIDDEN = args['hidden']
FEATHIDDEN = args['feat_hidden']
BASES = args['num_basis_functions']
LR = args['learning_rate']
decay_rate = args['decay_rate']
consecutive_threshold = args['consecutive']
WRITESUMMARY = args['write_summary']
SUMMARIESDIR = args['summaries_dir']
FEATURES = args['features']
SYM = args['norm_symmetric']
TESTING = args['testing']
ACCUM = args['accumulation']
NUM_LAYERS = args['num_layers']
GCMC_INDICES = args['use_gcmc_indices']
DROPOUT_EDGES = args['dropout_edges']
normalise_edges = args['normalise_edges']
observed_fraction = args['observed_fraction']
val_fraction = args['val_fraction']
test_fraction = args['test_fraction']

SELFCONNECTIONS = False
SPLITFROMFILE = True
VERBOSE = True

if DATASET == 'ml_1m' or DATASET == 'ml_100k' or DATASET == 'douban':
	NUMCLASSES = 5
elif DATASET == 'ml_10m':
	NUMCLASSES = 10
	print('\n WARNING: this might run out of RAM, consider using train_minibatch.py for dataset %s' % DATASET)
	print('If you want to proceed with this option anyway, uncomment this.\n')
	sys.exit(1)
elif DATASET == 'flixster':
	NUMCLASSES = 10
elif DATASET == 'yahoo_music':
	NUMCLASSES = 71
	if ACCUM == 'sum':
		print('\n WARNING: combining DATASET=%s with ACCUM=%s can cause memory issues due to large number of classes.')
		print('Consider using "--accum stack" as an option for this dataset.')
		print('If you want to proceed with this option anyway, uncomment this.\n')
		sys.exit(1)
elif DATASET == 'facebook':
	FEATURES == True
	NUMCLASSES = 2

# Splitting dataset in training, validation and test set

if DATASET == 'ml_1m' or DATASET == 'ml_10m':
	if FEATURES:
		datasplit_path = 'data/' + DATASET + '/withfeatures_split_seed' + str(DATASEED) + '.pickle'
	else:
		datasplit_path = 'data/' + DATASET + '/split_seed' + str(DATASEED) + '.pickle'
elif FEATURES:
	datasplit_path = 'data/' + DATASET + '/withfeatures.pickle'
else:
	datasplit_path = 'data/' + DATASET + '/nofeatures.pickle'


if DATASET == 'flixster' or DATASET == 'douban' or DATASET == 'yahoo_music':
	u_features, v_features, adj_train, train_labels, train_u_indices, train_v_indices, \
		val_labels, val_u_indices, val_v_indices, test_labels, \
		test_u_indices, test_v_indices, class_values = load_data_monti(DATASET, TESTING)

elif DATASET == 'ml_100k':
	print("Using official MovieLens dataset split u1.base/u1.test with 20% validation set size...")
	u_features, v_features, adj_train, train_labels, train_u_indices, train_v_indices, \
		val_labels, val_u_indices, val_v_indices, test_labels, \
		test_u_indices, test_v_indices, class_values = load_official_trainvaltest_split(DATASET, TESTING)

elif DATASET == 'facebook':
	adj = np.load('data/facebook/adjacency2.npy')
	u_features = sp.csr_matrix(np.load('data/facebook/user_features.npy'))
	v_features = sp.csr_matrix(np.load('data/facebook/user_features.npy'))
	edges_fraction = 0.01 # this gives around as many non-edges as connected edges.
	# adj_train, train_labels, train_u_indices, train_v_indices, \
	# 	val_labels, val_u_indices, val_v_indices, test_labels, \
	# 	test_u_indices, test_v_indices, class_values = load_facebook_data(adj, observed_fraction, edges_fraction)

	adj_train, train_labels, train_u_indices, train_v_indices, \
		val_labels, val_u_indices, val_v_indices, test_labels, \
		test_u_indices, test_v_indices, class_values = load_facebook_data_link_prediction(adj, edges_fraction, val_fraction, test_fraction)

else:
	print("Using random dataset split ...")
	u_features, v_features, adj_train, train_labels, train_u_indices, train_v_indices, \
		val_labels, val_u_indices, val_v_indices, test_labels, \
		test_u_indices, test_v_indices, class_values = create_trainvaltest_split(DATASET, DATASEED, TESTING,
																				 datasplit_path, SPLITFROMFILE,
																				 VERBOSE)

num_users, num_items = adj_train.shape

num_side_features = 0

# feature loading
if not FEATURES:
	u_features = sp.identity(num_users, format='csr') # features is just one-hot vector!
	v_features = sp.identity(num_items, format='csr')

	u_features, v_features = preprocess_user_item_features(u_features, v_features)

elif FEATURES and u_features is not None and v_features is not None:
	# use features as side information and node_id's as node input features

	print("Normalizing feature vectors...")
	u_features_side = normalize_features(u_features)
	v_features_side = normalize_features(v_features)

	u_features_side, v_features_side = preprocess_user_item_features(u_features_side, v_features_side)

	u_features_side = np.array(u_features_side.todense(), dtype=np.float32)
	v_features_side = np.array(v_features_side.todense(), dtype=np.float32)

	num_side_features = u_features_side.shape[1]

	# node id's for node input features
	id_csr_v = sp.identity(num_items, format='csr')
	id_csr_u = sp.identity(num_users, format='csr')

	u_features, v_features = preprocess_user_item_features(id_csr_u, id_csr_v)

else:
	raise ValueError('Features flag is set to true but no features are loaded from dataset ' + DATASET)

print("User features shape: " + str(u_features.shape))
print("Item features shape: " + str(v_features.shape))
print("adj_train shape: " + str(adj_train.shape))


# global normalization
support = []
support_t = []
adj_train_int = sp.csr_matrix(adj_train, dtype=np.int32)

for i in range(NUMCLASSES):
	# build individual binary rating matrices (supports) for each rating
	support_unnormalized = sp.csr_matrix(adj_train_int == i + 1, dtype=np.float32)

	if support_unnormalized.nnz == 0 and DATASET != 'yahoo_music':
		# yahoo music has dataset split with not all ratings types present in training set.
		# this produces empty adjacency matrices for these ratings.
		sys.exit('ERROR: normalized bipartite adjacency matrix has only zero entries!!!!!')

	support_unnormalized_transpose = support_unnormalized.T
	support.append(support_unnormalized)
	support_t.append(support_unnormalized_transpose)


support = globally_normalize_bipartite_adjacency(support, symmetric=SYM)
support_t = globally_normalize_bipartite_adjacency(support_t, symmetric=SYM)

if SELFCONNECTIONS:
	support.append(sp.identity(u_features.shape[0], format='csr'))
	support_t.append(sp.identity(v_features.shape[0], format='csr'))

num_support = len(support)
support = sp.hstack(support, format='csr')
support_t = sp.hstack(support_t, format='csr')
# support and support_t become 3000x15000 (for douban with 3000 users/items and 5 ratings)
# support is n_users x (n_items*n_ratings). support_t is n_items x (n_users*ratings)
# NOTE: support is sparse matrix so the shape may not be as large as expected (?)
# When is num_support ever not == num_rating_classes?
print('support shape: ' + str(support.shape))
print('support_t shape: ' + str(support_t.shape))

if ACCUM == 'stack' or ACCUM == 'stackRGGCN':
	div = HIDDEN[0] // num_support
	if HIDDEN[0] % num_support != 0:
		print("""\nWARNING: HIDDEN[0] (=%d) of stack layer is adjusted to %d such that
				  it can be evenly split in %d splits.\n""" % (HIDDEN[0], num_support * div, num_support))
	HIDDEN[0] = num_support * div

##################################################################################################################
""" support contains only training set ratings. index into support using user/item indices to create test set support. """
test_support = val_support = train_support = support
test_support_t = val_support_t = train_support_t = support_t

if GCMC_INDICES:
	# Collect all user and item nodes for test set
	test_u = list(set(test_u_indices))
	test_v = list(set(test_v_indices))
	test_support = support[np.array(test_u)]
	test_support_t = support_t[np.array(test_v)]

	# Collect all user and item nodes for validation set
	val_u = list(set(val_u_indices))
	val_v = list(set(val_v_indices))
	val_support = support[np.array(val_u)]
	val_support_t = support_t[np.array(val_v)]

	# Collect all user and item nodes for train set
	train_u = list(set(train_u_indices))
	train_v = list(set(train_v_indices))
	train_support = support[np.array(train_u)]
	train_support_t = support_t[np.array(train_v)]

	test_u_dict = {n: i for i, n in enumerate(test_u)}
	test_v_dict = {n: i for i, n in enumerate(test_v)}
	test_u_indices = np.array([test_u_dict[o] for o in test_u_indices])
	test_v_indices = np.array([test_v_dict[o] for o in test_v_indices])
	
	val_u_dict = {n: i for i, n in enumerate(val_u)}
	val_v_dict = {n: i for i, n in enumerate(val_v)}
	val_u_indices = np.array([val_u_dict[o] for o in val_u_indices])
	val_v_indices = np.array([val_v_dict[o] for o in val_v_indices])

	train_u_dict = {n: i for i, n in enumerate(train_u)}
	train_v_dict = {n: i for i, n in enumerate(train_v)}
	print('max train_u_indices: {}'.format(max(train_u_indices)))
	train_u_indices = np.array([train_u_dict[o] for o in train_u_indices]) ### HERE IS WHERE indices get changed to suit the new indexing into smaller set of users
	train_v_indices = np.array([train_v_dict[o] for o in train_v_indices])
	print('max train_u_indices after: {}'.format(max(train_u_indices)))

print('train_support_shape: {}'.format(train_support.shape)) # if GCMC_INDICES, THIS IS NO LONGER (n_users, n_items*n_rating_types). but < n_users
##################################################################################################################

# features as side info
if FEATURES:
	if GCMC_INDICES:
		test_u_features_side = u_features_side[np.array(test_u)]
		test_v_features_side = v_features_side[np.array(test_v)]

		val_u_features_side = u_features_side[np.array(val_u)]
		val_v_features_side = v_features_side[np.array(val_v)]

		train_u_features_side = u_features_side[np.array(train_u)]
		train_v_features_side = v_features_side[np.array(train_v)]
	else:
		test_u_features_side = u_features_side
		test_v_features_side = v_features_side

		val_u_features_side = u_features_side
		val_v_features_side = v_features_side

		train_u_features_side = u_features_side
		train_v_features_side = v_features_side

else:
	test_u_features_side = None
	test_v_features_side = None

	val_u_features_side = None
	val_v_features_side = None

	train_u_features_side = None
	train_v_features_side = None

placeholders = {
	'u_features': tf.sparse_placeholder(tf.float32, shape=np.array(u_features.shape, dtype=np.int64)),
	'v_features': tf.sparse_placeholder(tf.float32, shape=np.array(v_features.shape, dtype=np.int64)),
	'u_features_nonzero': tf.placeholder(tf.int32, shape=()),
	'v_features_nonzero': tf.placeholder(tf.int32, shape=()),
	'labels': tf.placeholder(tf.int32, shape=(None,)),

	'u_features_side': tf.placeholder(tf.float32, shape=(None, num_side_features)),
	'v_features_side': tf.placeholder(tf.float32, shape=(None, num_side_features)),

	'user_indices': tf.placeholder(tf.int32, shape=(None,)),
	'item_indices': tf.placeholder(tf.int32, shape=(None,)),

	'class_values': tf.placeholder(tf.float32, shape=class_values.shape),

	'dropout': tf.placeholder_with_default(0., shape=()),
	'weight_decay': tf.placeholder_with_default(0., shape=()),

	'support': tf.sparse_placeholder(tf.float32, shape=(None, None)),
	'support_t': tf.sparse_placeholder(tf.float32, shape=(None, None)),
}

##################################################################################################################
E_start, E_end = get_edges_matrices(adj_train, normalise_edges, DO)
# E_start = sp.hstack(E_start, format='csr')  # confirm if vstack is correct and not hstack
# E_end = sp.hstack(E_end, format='csr')

# placeholders['E_start'] = tf.sparse_placeholder(tf.float32, shape=(None, None, None))
# placeholders['E_end'] = tf.sparse_placeholder(tf.float32, shape=(None, None, None))

placeholders['E_start_list'] = []
placeholders['E_end_list'] = []
placeholders['E_start_nonzero_list'] = []
placeholders['E_end_nonzero_list'] = []
for i in range(num_support):
	placeholders['E_start_list'].append(tf.sparse_placeholder(tf.float32, shape=(None, None), name='CULPRIT'))
	placeholders['E_end_list'].append(tf.sparse_placeholder(tf.float32, shape=(None, None)))
	placeholders['E_start_nonzero_list'].append(tf.placeholder(tf.int32, shape=()))
	placeholders['E_end_nonzero_list'].append(tf.placeholder(tf.int32, shape=()))
# print('shape of E_end for first rating type: {}'.format(E_end[0].toarray().shape))

##################################################################################################################


# create model
if FEATURES:
	model = RecommenderSideInfoGAE(placeholders,
								   input_dim=u_features.shape[1],
								   feat_hidden_dim=FEATHIDDEN,
								   num_classes=NUMCLASSES,
								   num_support=num_support,
								   self_connections=SELFCONNECTIONS,
								   num_basis_functions=BASES,
								   hidden=HIDDEN,
								   num_users=num_users,
								   num_items=num_items,
								   accum=ACCUM,
								   learning_rate=LR,
								   num_side_features=num_side_features,
								   num_layers=NUM_LAYERS,
								   dropout_edges=DROPOUT_EDGES,
								   logging=True)
else:
	model = RecommenderGAE(placeholders,
						   input_dim=u_features.shape[1],
						   num_classes=NUMCLASSES,
						   num_support=num_support,
						   self_connections=SELFCONNECTIONS,
						   num_basis_functions=BASES,
						   hidden=HIDDEN,
						   num_users=num_users,
						   num_items=num_items,
						   accum=ACCUM,
						   learning_rate=LR,
						   num_layers=NUM_LAYERS,
						   dropout_edges=DROPOUT_EDGES,
						   logging=True)

# Convert sparse placeholders to tuples to construct feed_dict. sparse placeholders expect tuple of (indices, values, shape)
test_support = sparse_to_tuple(test_support)
test_support_t = sparse_to_tuple(test_support_t)

val_support = sparse_to_tuple(val_support)
val_support_t = sparse_to_tuple(val_support_t)

train_support = sparse_to_tuple(train_support)
train_support_t = sparse_to_tuple(train_support_t)

u_features = sparse_to_tuple(u_features)
v_features = sparse_to_tuple(v_features)
assert u_features[2][1] == v_features[2][1], 'Number of features of users and items must be the same!'

num_features = u_features[2][1]
u_features_nonzero = u_features[1].shape[0]
v_features_nonzero = v_features[1].shape[0]

# setting E_start to be the same for train, val, and test. E_start already only contains train edges (from preprocessing script)
train_E_start = []
train_E_end = []
E_start_nonzero_list = []
E_end_nonzero_list = []
print('LENGTH OF E_START: {}'.format(len(E_start)))
print('NUM_SUPPORT: {}'.format(num_support))
for i in range(num_support):
	E_start_i = sparse_to_tuple(E_start[i])
	E_end_i = sparse_to_tuple(E_end[i])
	train_E_start.append(E_start_i)
	train_E_end.append(E_end_i)
	E_start_nonzero_list.append(E_start_i[1].shape[0])
	E_end_nonzero_list.append(E_end_i[1].shape[0])
val_E_start = test_E_start = train_E_start
val_E_end = test_E_end = train_E_end


# Feed_dicts for validation and test set stay constant over different update steps
train_feed_dict = construct_feed_dict(placeholders, u_features, v_features, u_features_nonzero,
									  v_features_nonzero, train_support, train_support_t,
									  train_labels, train_u_indices, train_v_indices, class_values, DO,
									  train_u_features_side, train_v_features_side, train_E_start, train_E_end, E_start_nonzero_list, E_end_nonzero_list)

# No dropout for validation and test runs. DO = dropout. input for val and test is same u_features and v_features.
val_feed_dict = construct_feed_dict(placeholders, u_features, v_features, u_features_nonzero,
									v_features_nonzero, val_support, val_support_t,
									val_labels, val_u_indices, val_v_indices, class_values, 0.,
									val_u_features_side, val_v_features_side, val_E_start, val_E_end, E_start_nonzero_list, E_end_nonzero_list)

test_feed_dict = construct_feed_dict(placeholders, u_features, v_features, u_features_nonzero,
									 v_features_nonzero, test_support, test_support_t,
									 test_labels, test_u_indices, test_v_indices, class_values, 0.,
									 test_u_features_side, test_v_features_side, test_E_start, test_E_end, E_start_nonzero_list, E_end_nonzero_list)


# Collect all variables to be logged into summary
merged_summary = tf.summary.merge_all()

sess = tf.Session()
initializers = tf.group(tf.global_variables_initializer(), tf.local_variables_initializer())
sess.run(initializers)

if WRITESUMMARY:
	train_summary_writer = tf.summary.FileWriter(SUMMARIESDIR + '/train', sess.graph)
	val_summary_writer = tf.summary.FileWriter(SUMMARIESDIR + '/val')
else:
	train_summary_writer = None
	val_summary_writer = None

best_val_score = np.inf
best_val_loss = np.inf
best_epoch = 0
wait = 0

print('Training...')

#### COUTNING PARAMS
total_parameters = 0
for variable in tf.trainable_variables():
	# shape is an array of tf.Dimension
	shape = variable.get_shape()
	# print(shape)
	# print(len(shape))
	variable_parameters = 1
	for dim in shape:
		# print(dim)
		variable_parameters *= dim.value
	# print(variable_parameters)
	total_parameters += variable_parameters
print('Total params: {}'.format(total_parameters))

# FOR A VARIABLE LEARNING RATE
assign_placeholder = tf.placeholder(tf.float32)
assign_op = model.learning_rate.assign(assign_placeholder)
old_loss = float('inf')
print('Original learning rate is {}'.format(sess.run(model.optimizer._lr)))

for epoch in range(NB_EPOCH):

	t = time.time()

	# Run single weight update
	# outs = sess.run([model.opt_op, model.loss, model.rmse], feed_dict=train_feed_dict)
	# with exponential moving averages
	outs = sess.run([model.training_op, model.loss, model.rmse], feed_dict=train_feed_dict)

	train_avg_loss = outs[1]
	train_rmse = outs[2]

	val_avg_loss, val_rmse = sess.run([model.loss, model.rmse], feed_dict=val_feed_dict)

	# if train_avg_loss > 0.999*old_loss:
	# 	consecutive += 1
	# 	if consecutive >= consecutive_threshold:
	# 		LR /= decay_rate
	# 		sess.run(assign_op, feed_dict={assign_placeholder: LR})
	# 		print('New learning rate is {}'.format(sess.run(model.optimizer._lr)))
	# 		consecutive = 0
	# else:
	# 	consecutive = 0
	# old_loss = train_avg_loss

	if VERBOSE:
		print("[*] Epoch:", '%04d' % (epoch + 1), "train_loss=", "{:.5f}".format(train_avg_loss),
			  "train_rmse=", "{:.5f}".format(train_rmse),
			  "val_loss=", "{:.5f}".format(val_avg_loss),
			  "val_rmse=", "{:.5f}".format(val_rmse),
			  "\t\ttime=", "{:.5f}".format(time.time() - t))

	if val_rmse < best_val_score:
		best_val_score = val_rmse
		best_epoch = epoch

	if epoch % 20 == 0 and WRITESUMMARY:
		# Train set summary
		summary = sess.run(merged_summary, feed_dict=train_feed_dict)
		train_summary_writer.add_summary(summary, epoch)
		train_summary_writer.flush()

		# Validation set summary
		summary = sess.run(merged_summary, feed_dict=val_feed_dict)
		val_summary_writer.add_summary(summary, epoch)
		val_summary_writer.flush()

	if epoch % 100 == 0 and epoch > 1000 and not TESTING and False:
		saver = tf.train.Saver()
		save_path = saver.save(sess, "tmp/%s_seed%d.ckpt" % (model.name, DATASEED), global_step=model.global_step)

		# load polyak averages
		variables_to_restore = model.variable_averages.variables_to_restore()
		saver = tf.train.Saver(variables_to_restore)
		saver.restore(sess, save_path)

		val_avg_loss, val_rmse = sess.run([model.loss, model.rmse], feed_dict=val_feed_dict)

		print('polyak val loss = ', val_avg_loss)
		print('polyak val rmse = ', val_rmse)

		# Load back normal variables
		saver = tf.train.Saver()
		saver.restore(sess, save_path)


# store model including exponential moving averages
saver = tf.train.Saver()
save_path = saver.save(sess, "tmp/%s.ckpt" % model.name, global_step=model.global_step)


if VERBOSE:
	print("\nOptimization Finished!")
	print('best validation score =', best_val_score, 'at iteration', best_epoch)


if TESTING:
	test_avg_loss, test_rmse, test_accuracy, test_precision, test_recall = sess.run([model.loss, model.rmse, model.accuracy, model.precision, model.recall], feed_dict=test_feed_dict)
	print('test loss = ', test_avg_loss)
	print('test rmse = ', test_rmse)
	print('test accuracy = ', test_accuracy)
	print('test precision = ', test_precision)
	print('test recall = ', test_recall)

	# restore with polyak averages of parameters
	variables_to_restore = model.variable_averages.variables_to_restore()
	saver = tf.train.Saver(variables_to_restore)
	saver.restore(sess, save_path)

	test_avg_loss, test_rmse, test_accuracy, test_precision, test_recall = sess.run([model.loss, model.rmse, model.accuracy, model.precision, model.recall], feed_dict=test_feed_dict)
	print('polyak test loss = ', test_avg_loss)
	print('polyak test rmse = ', test_rmse)
	print('polyak test accuracy = ', test_accuracy)
	print('polyak test precision = ', test_precision)
	print('polyak test recall = ', test_recall)

else:
	# restore with polyak averages of parameters
	variables_to_restore = model.variable_averages.variables_to_restore()
	saver = tf.train.Saver(variables_to_restore)
	saver.restore(sess, save_path)

	val_avg_loss, val_rmse, val_accuracy, val_precision, val_recall = sess.run([model.loss, model.rmse, model.accuracy, model.precision, model.recall], feed_dict=val_feed_dict)
	print('polyak val loss = ', val_avg_loss)
	print('polyak val rmse = ', val_rmse)
	print('polyak val accuracy = ', val_accuracy)
	print('polyak val precision = ', val_precision)
	print('polyak val recall = ', val_recall)

print('\nSETTINGS:\n')
for key, val in sorted(vars(ap.parse_args()).items()):
	print(key, val)

print('global seed = ', seed)

# For parsing results from file
results = vars(ap.parse_args()).copy()
results.update({'best_val_score': float(best_val_score), 'best_epoch': best_epoch})
print(json.dumps(results)) # dumps just dumps into a string, not for saving into a file

sess.close()
