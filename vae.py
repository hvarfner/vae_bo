import sys
import os
import time
import matplotlib
import matplotlib.pyplot as plt
import jax
import jax.numpy as jnp
from jax import jit, grad, lax, random
from jax.experimental import optimizers
from jax.experimental import stax
from jax.experimental.stax import Dense, FanOut, Relu, Softplus,\
     Sigmoid, Conv, BatchNorm, Flatten, ConvTranspose
from jax.random import multivariate_normal
import numpy as np
import tensorflow_probability as tfp
from data import load_mnist
from utils import plot_latent_space

LATENT_SIZE = 2
IMAGE_SHAPE = (28, 28)

def Reshape(new_shape):
    """Layer construction function for flattening all but the leading dim."""
    def init_fun(rng, input_shape):
        output_shape = (input_shape[0],) + new_shape
        return output_shape, ()
    def apply_fun(params, inputs, **kwargs):
        output_shape = (inputs.shape[0],) + new_shape
        return jnp.reshape(inputs, output_shape)
    return init_fun, apply_fun


def gaussian_kl(mu, sigmasq):
    return -0.5 * jnp.sum(1. + jnp.log(sigmasq) - mu**2. - sigmasq)


def gaussian_sample(rng, mu, sigmasq):
    return mu + jnp.sqrt(sigmasq) * random.normal(rng, mu.shape)


# TODO change this when doing regression or softmax?
# TODO understand the where fully
def bernoulli_logpdf(logits, x):
    return -jnp.sum(jnp.logaddexp(0., jnp.where(x, -1., 1.) * logits))


def sample_latent_space(rng, params, images):
    encoder_params, decoder_params = params
    mu_z, sigmasq_z = encode(encoder_params, images)
    sample = gaussian_sample(rng, mu_z, sigmasq_z)
    return sample
    

def elbo(rng, params, images, beta=1):
    encoder_params, decoder_params = params
    mu_z, sigmasq_z = encode(encoder_params, images)
    sample = gaussian_sample(rng, mu_z, sigmasq_z)
    logits = decode(decoder_params, sample)
    return bernoulli_logpdf(logits, images) - beta * gaussian_kl(mu_z, sigmasq_z)


# TODO create the iwelbo as well
def iwelbo(rng, params, images, n_samples=10):
    pass


def image_sample(rng, params, nrow, ncol):    
    _, decoder_params = params
    code_rng, image_rng = random.split(rng)
    # samples from the standard normal in latent space with shape (nrow * ncol, 10)
    latent_sample = random.normal(code_rng, (nrow * ncol, LATENT_SIZE))
    logits = decode(decoder_params, latent_sample)
    sampled_images = random.bernoulli(image_rng, jnp.logaddexp(0., logits))
    return image_grid(nrow, ncol, sampled_images, IMAGE_SHAPE)


def image_grid(nrow, ncol, image_vectors, image_shape):
  images = iter(image_vectors.reshape((-1,) + image_shape))
  return jnp.vstack([jnp.hstack([next(images).T for _ in range(ncol)][::-1])
                    for _ in range(nrow)]).T


# define the VAE - one of FanOuts is softplus due to non-negative variance
def init_vanilla_vae():
    encoder_init, encode = stax.serial(
        Dense(512), Relu,
        Dense(512), Relu,
        FanOut(2),
        stax.parallel(Dense(LATENT_SIZE), stax.serial(Dense(LATENT_SIZE), Softplus)),
    )

    decoder_init, decode = stax.serial(
        Dense(512), Relu,
        Dense(512), Relu,
        Dense(np.prod(IMAGE_SHAPE))
    )
    return encoder_init, encode, decoder_init, decode


def init_conv_vae():
    encoder_init, encode = stax.serial(Conv(32, (3, 3), padding="SAME"), Relu,
                                 Conv(64, (3, 3), (2, 2), padding="SAME"), Relu,
                                 Conv(64, (3, 3), padding="SAME"), Relu,
                                 Conv(64, (3, 3), padding="SAME"), Relu,
                                 Flatten,
                                 Dense(32), Relu,
                                 FanOut(2),
                                 stax.parallel(Dense(LATENT_SIZE), stax.serial(Dense(LATENT_SIZE), Softplus)),
    )
    decoder_init, decode = stax.serial(Dense(12544), Relu,
                                    Reshape((14, 14, 64)),
                                    ConvTranspose(32, (3, 3), (2, 2), padding="SAME"), Relu,
                                    Conv(1, (1, 1), padding="SAME"), Relu)

    return encoder_init, encode, decoder_init, decode


if __name__ == '__main__':
    vae_type = sys.argv[1]
    reshape = vae_type == 'vanilla'
    beta = 1
    step_size = 0.001
    num_epochs = 50
    batch_size = 16
    nrow, ncol = 10, 10  # sampled image grid size
    test_rng = random.PRNGKey(1)  # fixed prng key for evaluation
    train_images, train_labels = load_mnist(train=True, reshape=reshape)
    test_images, test_labels = load_mnist(train=False)
    num_complete_batches, leftover = divmod(train_images.shape[0], batch_size)
    num_batches = num_complete_batches + bool(leftover)
    
    imfile = os.path.join(os.path.join(os.getcwd(), "tmp/"), "mnist_vae_{:03d}.png")
    encoder_init_rng, decoder_init_rng = random.split(random.PRNGKey(2))
    if vae_type == 'vanilla':
        define_vae = init_vanilla_vae
        input_shape = (batch_size, np.prod(IMAGE_SHAPE))
    else:
        define_vae = init_conv_vae
        input_shape = (batch_size, ) + IMAGE_SHAPE + (1, )

    encoder_init, encode, decoder_init, decode = define_vae()
    _, encoder_init_params = encoder_init(encoder_init_rng, input_shape)
    _, decoder_init_params = decoder_init(decoder_init_rng, (batch_size, LATENT_SIZE))
    init_params = (encoder_init_params, decoder_init_params)

    opt_init, opt_update, get_params = optimizers.momentum(step_size, mass=0.9)
    train_images = jax.device_put(train_images[0:100])
    test_images = jax.device_put(test_images[0:50])
    test_labels = test_labels[0:500]


    def binarize_batch(rng, i, images):
        i  = i % num_batches
        batch = lax.dynamic_slice_in_dim(images, i * batch_size, batch_size)
        return random.bernoulli(rng, batch).astype(jnp.float32)


    @jit
    def run_epoch(rng, opt_state, images):
        def body_fun(i, opt_state):
            elbo_rng, data_rng = random.split(random.fold_in(rng, i))
            batch = binarize_batch(data_rng, i, images)
            loss = lambda params: -elbo(elbo_rng, params, batch, beta=beta) / batch_size
            grads = grad(loss)(get_params(opt_state))
            return opt_update(i, grads, opt_state)
        return lax.fori_loop(0, num_batches, body_fun, opt_state)


    @jit
    def evaluate(opt_state, images):
        params = get_params(opt_state)
        elbo_rng, data_rng, image_rng = random.split(test_rng, 3)
        binarized_test = random.bernoulli(data_rng, test_images)
        test_elbo = elbo(elbo_rng, params, binarized_test, beta=1) / images.shape[0]
        sampled_images = image_sample(image_rng, params, nrow, ncol)
        latent_samples = sample_latent_space(elbo_rng, params, images)
        return test_elbo, sampled_images, latent_samples

    opt_state = opt_init(init_params)
    for epoch in range(num_epochs):
        tic = time.time()
        opt_state = run_epoch(random.PRNGKey(epoch), opt_state, train_images)
        test_elbo, sampled_images, latent_samples = evaluate(opt_state, test_images)
        print("Ep. {: 3d} ---- ELBO: {} ---- Time: {:.3f} sec".format(epoch, -test_elbo, time.time() - tic))
        plt.imsave(imfile.format(epoch), sampled_images, cmap=plt.cm.gray)
    plt.scatter(latent_samples[:, 0], latent_samples[:, 1], c=test_labels,\
        cmap='viridis')
    plt.show()

# TODO CHECK IN ENCODE WHAT SHAPE STUFF IS!