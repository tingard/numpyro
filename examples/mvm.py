from functools import partial
from jax import vmap, jit, custom_jvp, value_and_grad, jvp, grad
import jax.numpy as np
import numpyro
from numpy.testing import assert_allclose
import time
import numpy as onp
from utils import kdot


def _get_chunks(L, chunk_size):
    num_chunks = L // chunk_size
    chunks = [np.arange(i * chunk_size, (i + 1) * chunk_size) for i in range(num_chunks)]
    if L % chunk_size != 0:
        chunks.append(np.arange(L - L % chunk_size, L))
    return chunks

def _chunk_vmap(fun, array, chunk_size=10):
    L = array.shape[0]
    if chunk_size >= L:
        return vmap(fun)(array)
    chunks = _get_chunks(L, chunk_size)
    results = [vmap(fun)(array[chunk]) for chunk in chunks]
    return np.concatenate(results)

# do a matrix vector after first materializing the matrix M
def vanilla_mvm(row):
    def do_mvm(rhs):
        M = vmap(row)(np.arange(N))
        return np.matmul(M, rhs)
    return do_mvm

# do a matrix vector multiply chunk-by-chunk
def partitioned_mvm(row, dilation):
    def do_mvm(rhs):
        @jit
        def compute_element(i):
            return np.dot(rhs, row(i))
        return _chunk_vmap(compute_element, np.arange(rhs.shape[-1]), rhs.shape[-1] // dilation)
    return do_mvm


# np.square(1.0 + kdot(kX, kX))
def kXkXsq_row(i, kX):
    return np.square(1.0 + np.matmul(kX, kX[i]))

def kXkXsq_mvm(b, kX, dilation=2):
    return partitioned_mvm(lambda i: kXkXsq_row(i, kX), dilation)(b)

# kdot(kX, kX) * kdot(kX, dkX)
def kXdkXsq_row(i, kX, dkX):
    return np.matmul(kX, kX[i]) * np.matmul(kX, dkX[i])

def kXdkXsq_mvm(b, kX, dkX, dilation=2):
    return partitioned_mvm(lambda i: kXdkXsq_row(i, kX, dkX), dilation)(b)

def partitioned_mvm2(row, P, dilation):
    def do_mvm(rhs):
        @jit
        def compute_element(i):
            return np.dot(rhs, row(i))
        return _chunk_vmap(compute_element, np.arange(P), P // dilation)
    return do_mvm

def kX_mvm(b, kX, dilation=2):
    return np.transpose(partitioned_mvm2(lambda i: kX[:, i], kX.shape[-1], dilation)(b))

def kX_mvm2(b, kX, dilation=2):
    @jit
    def compute_element(i):
        return np.dot(b, kX[i, :])
    return _chunk_vmap(compute_element, np.arange(kX.shape[0]), kX.shape[0] // dilation)

def quad_mvm(b, X):
    return np.einsum('np,p->n', X, np.einsum('np,n->p', X, b))

def quad_mvm_dil(b, X, dilation=2):
    N, P = X.shape
    @jit
    def compute_element1(i):
        return np.dot(X[:, i], b)
    partial = _chunk_vmap(compute_element1, np.arange(P), P // dilation)
    @jit
    def compute_element2(i):
        return np.dot(X[i], partial)
    return _chunk_vmap(compute_element2, np.arange(N), N // dilation)

def kernel(X, Z, eta1, eta2, c):
    eta1sq = np.square(eta1)
    eta2sq = np.square(eta2)
    k1 = 0.5 * eta2sq * np.square(1.0 + kdot(X, Z))
    k2 = -0.5 * eta2sq * kdot(np.square(X), np.square(Z))
    k3 = (eta1sq - eta2sq) * kdot(X, Z)
    k4 = np.square(c) - 0.5 * eta2sq
    return k1 + k2 + k3 + k4

def kernel_mvm_diag(b, kX, eta1, eta2, c, diag, dilation=2):
    eta1sq = np.square(eta1)
    eta2sq = np.square(eta2)
    k1b = 0.5 * eta2sq * kXkXsq_mvm(b, kX, dilation=dilation)
    k2b = -0.5 * eta2sq * quad_mvm_dil(b, np.square(kX), dilation=dilation)
    k3b = (eta1sq - eta2sq) * quad_mvm_dil(b, kX, dilation=dilation)
    k4b = (np.square(c) - 0.5 * eta2sq) * np.sum(b) * np.ones(b.shape)
    return k1b + k2b + k3b + k4b + diag * b

@custom_jvp
@partial(custom_jvp, nondiff_argnums=(0, 2, 5, 6))
def kernel_mvm(b, kappa, X, eta1, eta2, c, dilation):
    return np.nan

@kernel_mvm.defjvp
def kernel_mvm_jvp(b, X, c, dilation, primals, tangents):
    kappa, eta1, eta2 = primals
    kappa_dot, eta1_dot, eta2_dot = tangents

    eta1sq = np.square(eta1)
    eta2sq = np.square(eta2)

    kX = kappa * X
    Xsq = np.square(X)
    dkX = kappa_dot * X
    dkXsq = kappa_dot * Xsq
    k3Xsq = kappa ** 3 * Xsq

    k1b = kXkXsq_mvm(b, kX, dilation=dilation)
    k2b = quad_mvm_dil(b, np.square(kX), dilation=dilation)
    k3b = quad_mvm_dil(b, kX, dilation=dilation)
    k4b = np.sum(b) * np.ones(b.shape)

    primal_out = 0.5 * eta2sq * k1b - 0.5 * eta2sq * k2b + (eta1sq - eta2sq) * k3b + \
                 (np.square(c) - 0.5 * eta2sq) * k4b

    b_dkX = kX_mvm(b, dkX, dilation=dilation)
    b_dkXsq = kX_mvm(b, dkXsq, dilation=dilation)
    kXdkXsq_b = np.transpose(kXdkXsq_mvm(b, kX, dkX, dilation=dilation))
    kX_b_dkX = kX_mvm2(b_dkX, kX, dilation=dilation)
    k3Xsq_b_dkXsq = kX_mvm2(b_dkXsq, k3Xsq, dilation=dilation)

    tangent_out_dkappa = 2.0 * eta1sq * kX_b_dkX - 2.0 * eta2sq * (k3Xsq_b_dkXsq - kXdkXsq_b)
    tangent_out_deta1 = 2.0 * eta1 * eta1_dot * k3b
    tangent_out_deta2 = eta2 * eta2_dot * (k1b - k2b - 2.0 * k3b - k4b)

    tangent_out = tangent_out_dkappa + tangent_out_deta1 + tangent_out_deta2

    return primal_out, tangent_out


if __name__ == "__main__":
    numpyro.set_platform("gpu")

    onp.random.seed(0)

    N = 9 * 10 ** 2
    P = 100
    b = np.sin(np.ones(N)) / N
    a = np.cos(np.ones(N)) / N

    eta1 = np.array(0.55)
    eta2 = np.array(0.22)
    c = 0.9

    kappa = np.array(onp.random.rand(P))

    dkX = np.array(onp.random.randn(N * P).reshape((N, P)))
    X = np.array(onp.random.randn(N * P).reshape((N, P)))

    def f(kappa, eta1, eta2):
        kX = kappa * X
        return np.matmul(kernel(kX, kX, eta1, eta2, c), b)

    def g(kappa, eta1, eta2):
        return kernel_mvm(b, kappa, X, eta1, eta2, c, 2)

    _, t1 = jvp(f, (kappa, eta1, eta2), (1.4 * kappa, 0.1, .2))
    _, t2 = jvp(g, (kappa, eta1, eta2), (1.4 * kappa, 0.1, .2))
    #assert_allclose(t1, t2, atol=1.0e-5, rtol=1.0e-5)
    #assert t1.shape == t2.shape
    _, t1 = jvp(f, (kappa, eta1, eta2), (1.4 / kappa, 0.3, .4))
    _, t2 = jvp(g, (kappa, eta1, eta2), (1.4 / kappa, 0.3, .4))
    #assert_allclose(t1, t2, atol=1.0e-5, rtol=1.0e-5)
    #assert t1.shape == t2.shape

    def f(_kappa, _eta1, _eta2):
        kX = _kappa * X
        return np.dot(a, np.matmul(kernel(kX, kX, _eta1, _eta2, c), b))

    def g(_kappa, _eta1, _eta2):
        kb = kernel_mvm(b, _kappa, X, np.broadcast_to(_eta1, b.shape), _eta2, c, 2)
        return np.dot(a, kb)

    def h(_kappa, _eta1, _eta2):
        return kernel_mvm(b, _kappa, X, np.broadcast_to(_eta1, b.shape), _eta2, c, 2)[0]

    v1, _ = value_and_grad(h, 1)(kappa, eta1, eta2)

    _, t1 = jvp(f, (kappa, eta1, eta2), (1.4 * kappa, 0.1, .2))
    _, t2 = jvp(g, (kappa, eta1, eta2), (1.4 * kappa, 0.1, .2))
    #assert_allclose(t1, t2, atol=1.0e-5, rtol=1.0e-5)

    g1 = grad(g, 1)(kappa, eta1, eta2)
    g2 = grad(f, 1)(kappa, eta1, eta2)
    assert_allclose(g1, g2, atol=1.0e-30, rtol=1.0e-30)

    import sys; sys.exit()

    v1, g1 = value_and_grad(g, 1)(kappa, eta1, eta2)
    v2, g2 = value_and_grad(f, 1)(kappa, eta1, eta2)
    assert_allclose(v1, v2, atol=1.0e-5, rtol=1.0e-5)
    assert_allclose(g1, g2, atol=1.0e-30, rtol=1.0e-30)

    v1, g1 = value_and_grad(g, 2)(kappa, eta1, eta2)
    v2, g2 = value_and_grad(f, 2)(kappa, eta1, eta2)
    assert_allclose(g1, g2, atol=1.0e-5, rtol=1.0e-5)

    import sys; sys.exit()

    v1, g1 = value_and_grad(kernel_quad, 1)(b, kappa, X, eta1, eta2, c, 2)
    v2, g2 = value_and_grad(f, 0)(kappa, eta1, eta2)
    assert_allclose(g1, g2, atol=1.0e-5, rtol=1.0e-5)

    import sys; sys.exit()

    res1 = quad_mvm(b, kX)
    res2 = quad_mvm_dil(b, kX, dilation=2)
    assert_allclose(res1, res2, atol=1.0e-5, rtol=1.0e-8)

    import sys; sys.exit()

    b2 = np.array(onp.random.randn(3,N))  # 3 N
    res1 = kX_mvm(b2, kX)   # 3 N
    res2 = np.matmul(b2, kX)              # 3 P
    assert_allclose(res1, res2, atol=1.0e-5, rtol=1.0e-5)

    import sys; sys.exit()

    kb1 = np.matmul(kernel(kX, kX, eta1, eta2, c), b)
    kb2 = kernel_mvm_diag(b, kX, eta1, eta2, c, dilation=2)
    kb3 = kernel_mvm_diag(b, kX, eta1, eta2, c, dilation=3)
    assert_allclose(kb1, kb2, atol=1.0e-5, rtol=1.0e-5)
    print("kb1 == kb2")
    assert_allclose(kb1, kb3, atol=1.0e-5, rtol=1.0e-5)
    print("kb1 == kb3")

    import sys; sys.exit()

    res1 = partitioned_mvm(lambda i: kXkXsq_row(i, kX), 8)(b)
    res2 = partitioned_mvm(lambda i: kXdkXsq_row(i, kX, dkX), 8)(b)

    if N < 10**4:
        res1v = vanilla_mvm(lambda i: kXkXsq_row(i, kX))(b)
        res1vv = np.matmul(np.square(1.0 + kdot(kX, kX)), b)
        assert_allclose(res1, res1v, atol=1.0e-3, rtol=1.0e-3)
        print("res1 == res1v")
        assert_allclose(res1, res1vv, atol=1.0e-3, rtol=1.0e-3)
        print("res1 == res1vv")

        res2v = vanilla_mvm(lambda i: kXdkXsq_row(i, kX, dkX))(b)
        res2vv = np.matmul(kdot(kX, kX) * kdot(dkX, kX), b)
        assert_allclose(res2, res2v, atol=1.0e-3, rtol=1.0e-3)
        print("res2 == res2v")
        assert_allclose(res2, res2vv, atol=1.0e-3, rtol=1.0e-3)
        print("res2 == res2vv")
