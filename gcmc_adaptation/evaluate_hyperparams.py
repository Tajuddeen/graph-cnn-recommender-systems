from main import run
from itertools import product
import pickle
import numpy as np

iterations = 2

NB_EPOCH = 20
DO = 0.5
HIDDEN = [50, 25]
NUM_LAYERS = 2

DATASET = 'facebook'
TESTING = True
VERBOSE = False
FEATURES = True
ACCUM = 'stack'
DROPOUT_EDGES = False
VF = 0.1
TF = 0.4

all_train_rmses, all_val_rmses, all_train_losses, all_val_losses, all_rmse, all_accuracy, all_precision, all_recall = [], [], [], [], [], [], [], []
experiment_name = '{} with {}, DE {}'.format(DATASET, ACCUM, DROPOUT_EDGES)
for i in range(iterations):
	print('runnning experiment for {}'.format(experiment_name))
	params_dict = {'DATASET': DATASET, 'FEATURES': FEATURES, 'NB_EPOCH': NB_EPOCH, 'DO': DO, 'HIDDEN': HIDDEN, 'NUM_LAYERS': NUM_LAYERS, 'TESTING': TESTING,
					'VERBOSE': VERBOSE, 'ACCUM': ACCUM, 'DROPOUT_EDGES': DROPOUT_EDGES, 'val_fraction': VF, 'test_fraction': TF}
	train_rmses, val_rmses, train_losses, val_losses, rmse, accuracy, precision, recall = run(**params_dict)
	all_train_rmses.append(train_rmses)
	all_val_rmses.append(val_rmses)
	all_train_losses.append(train_losses)
	all_val_losses.append(val_losses)
	all_rmse.append(rmse)
	all_accuracy.append(accuracy)
	all_precision.append(precision)
	all_recall.append(recall)

print('hyperparameter evaluation done!')

best_epochs = np.argmin(np.array(all_val_rmses), 1)
average = np.mean(all_rmse)
print('best epochs: {}'.format(best_epochs))
print('average rmse: {}'.format(average))
print('average accuracy: {}'.format(np.mean(all_accuracy)))
print('average precision: {}'.format(np.mean(all_precision)))
print('average recall: {}'.format(np.mean(all_recall)))