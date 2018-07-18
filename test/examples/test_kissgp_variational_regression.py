from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

from math import exp, pi

import os
import torch
import unittest
import gpytorch
from torch.autograd import Variable
from torch.utils.data import TensorDataset, DataLoader
from gpytorch.kernels import RBFKernel
from gpytorch.likelihoods import GaussianLikelihood
from gpytorch.means import ConstantMean
from gpytorch.priors import SmoothedBoxPrior
from gpytorch.random_variables import GaussianRandomVariable


# Simple training data: let's try to learn a sine function,
# but with KISS-GP let's use 100 training examples.
def make_data():
    train_x = torch.linspace(0, 1, 1000)
    train_y = torch.sin(train_x * (4 * pi)) + torch.randn(train_x.size()) * 0.2
    test_x = torch.linspace(0.02, 1, 51)
    test_y = torch.sin(test_x * (4 * pi))
    return train_x, train_y, test_x, test_y


class GPRegressionModel(gpytorch.models.GridInducingVariationalGP):
    def __init__(self):
        super(GPRegressionModel, self).__init__(grid_size=20, grid_bounds=[(-0.05, 1.05)])
        self.mean_module = ConstantMean(prior=SmoothedBoxPrior(-10, 10))
        self.covar_module = RBFKernel(
            log_lengthscale_prior=SmoothedBoxPrior(exp(-3), exp(6), sigma=0.1, log_transform=True)
        )
        self.register_parameter(
            name="log_outputscale",
            parameter=torch.nn.Parameter(torch.Tensor([0])),
            prior=SmoothedBoxPrior(exp(-5), exp(1), sigma=0.1, log_transform=True),
        )

    def forward(self, x):
        mean_x = self.mean_module(x)
        covar_x = self.covar_module(x) * self.log_outputscale.exp()
        return GaussianRandomVariable(mean_x, covar_x)


class TestKissGPVariationalRegression(unittest.TestCase):
    def setUp(self):
        if os.getenv("UNLOCK_SEED") is None or os.getenv("UNLOCK_SEED").lower() == "false":
            self.rng_state = torch.get_rng_state()
            torch.manual_seed(2)

    def tearDown(self):
        if hasattr(self, "rng_state"):
            torch.set_rng_state(self.rng_state)

    def test_kissgp_gp_mean_abs_error(self):
        train_x, train_y, test_x, test_y = make_data()
        train_dataset = TensorDataset(train_x, train_y)
        train_loader = DataLoader(train_dataset, shuffle=True, batch_size=64)

        model = GPRegressionModel()
        likelihood = GaussianLikelihood()
        mll = gpytorch.mlls.VariationalMarginalLogLikelihood(likelihood, model, n_data=len(train_y))
        n_iter = 40
        # We use SGD here, rather than Adam
        # Emperically, we find that SGD is better for variational regression
        optimizer = torch.optim.SGD([
            {'params': model.parameters()},
            {'params': likelihood.parameters()},
        ], lr=0.1)

        # We use a Learning rate scheduler from PyTorch to lower the learning rate during optimization
        # We're going to drop the learning rate by 1/10 after 3/4 of training
        # This helps the model converge to a minimum
        scheduler = torch.optim.lr_scheduler.MultiStepLR(optimizer, milestones=[0.75 * n_iter], gamma=0.1)

        # Our loss object
        # We're using the VariationalMarginalLogLikelihood object
        mll = gpytorch.mlls.VariationalMarginalLogLikelihood(likelihood, model, n_data=train_y.size(0))

        # The training loop
        def train():
            for i in range(n_iter):
                scheduler.step()
                for x_batch, y_batch in train_loader:
                    x_batch = torch.autograd.Variable(x_batch.float())
                    y_batch = torch.autograd.Variable(y_batch.float())
                    optimizer.zero_grad()
                    with gpytorch.settings.use_toeplitz(False), gpytorch.beta_features.diagonal_correction():
                        output = model(x_batch)
                        loss = -mll(output, y_batch)
                    loss.backward()
                    optimizer.step()

        train()

        for name, param in model.named_parameters():
            self.assertTrue(param.grad is not None)
            self.assertGreater(param.grad.norm().item(), 0)
        for param in likelihood.parameters():
            self.assertTrue(param.grad is not None)
            self.assertGreater(param.grad.norm().item(), 0)

            # Test the model
            model.eval()
            likelihood.eval()

            test_preds = likelihood(model(Variable(test_x))).mean()
            mean_abs_error = torch.mean(torch.abs(Variable(test_y) - test_preds))

        self.assertLess(mean_abs_error.data.squeeze().item(), 0.1)


if __name__ == "__main__":
    unittest.main()
