# Owner(s): ["oncall: fx"]

import contextlib
import pickle
from unittest.mock import patch

import torch
import torch._export
from torch import fx
from torch.fx.experimental.proxy_tensor import make_fx
from torch.fx.lazy_graph_module import _LazyGraphModule
from torch.testing._internal.common_utils import run_tests, TestCase


class TestLazyGraphModule(TestCase):
    exit_stack = None

    @classmethod
    def setUpClass(cls):
        cls.exit_stack = contextlib.ExitStack()
        cls.exit_stack.enter_context(fx._use_lazy_graph_module(True))

    @classmethod
    def tearDownClass(cls):
        cls.exit_stack.close()

    @staticmethod
    def replace_sin_with_cos(gm):
        for n in gm.graph.nodes:
            if n.target == "sin":
                n.target = "cos"

    def test_replace_sin_with_cos(self):
        def f(x):
            return x.sin()

        x = torch.randn(2, 3)
        gm = fx.symbolic_trace(f)
        self.replace_sin_with_cos(gm)

        gm.recompile()
        expected = x.cos()
        actual = gm(x)

        self.assertTrue(torch.allclose(expected, actual))
        code = gm.print_readable(False)
        self.assertTrue("cos()" in code)
        self.assertTrue(isinstance(gm, _LazyGraphModule))

    def test_call_forward_directly(self):
        def f(x):
            return x.sin()

        x = torch.randn(2, 3)
        gm = fx.symbolic_trace(f)
        self.assertTrue(isinstance(gm, _LazyGraphModule))
        self.replace_sin_with_cos(gm)
        gm.recompile()
        expected = x.cos()
        actual = gm.forward(x)

        self.assertTrue(torch.allclose(expected, actual))

    def test_needs_recompile(self):
        """
        Make sure needs_recompile() return the corrent state.
        """

        def f(x):
            return x.sin()

        gm = fx.symbolic_trace(f)
        self.assertTrue(isinstance(gm, _LazyGraphModule))
        self.assertTrue(gm._needs_recompile())
        gm(torch.randn(2, 3))
        self.assertFalse(gm._needs_recompile())

    def test_multi_recompile(self):
        """
        Cover the case that multiple recompilation happens.
        """

        def f(x):
            return x.sin()

        gm = fx.symbolic_trace(f)
        self.assertTrue(isinstance(gm, _LazyGraphModule))
        self.assertTrue(gm._needs_recompile())
        x = torch.randn(2, 3)
        # trigger the first recompilation
        self.assertTrue(torch.allclose(x.sin(), gm(x)))
        self.assertFalse(gm._needs_recompile())

        self.replace_sin_with_cos(gm)
        self.assertFalse(gm._needs_recompile())
        gm.recompile()
        self.assertTrue(gm._needs_recompile())
        # trigger the second recompilation
        self.assertTrue(torch.allclose(x.cos(), gm(x)))
        self.assertFalse(gm._needs_recompile())

    def test_accessing_code_cause_recompiling(self):
        """
        Make sure we recompile if we have not done that yet when we access the code
        property of a GraphModule.
        """

        def f(x):
            return x.sin()

        gm = fx.symbolic_trace(f)
        self.assertTrue(isinstance(gm, _LazyGraphModule))
        self.assertTrue(gm._needs_recompile())
        # should trigger a recompilation
        code = gm.code
        self.assertTrue("sin" in code)
        self.assertFalse(gm._needs_recompile())

    def test_graph_module_str(self):
        def f(x):
            return x.sin()

        gm = fx.symbolic_trace(f)
        self.assertTrue(isinstance(gm, _LazyGraphModule))
        self.assertTrue("sin" in str(gm))

    def test_recapture_with_make_fx(self):
        def f(x):
            return x.sin()

        gm = fx.symbolic_trace(f)
        self.assertTrue(isinstance(gm, _LazyGraphModule))
        self.assertTrue(gm._needs_recompile())
        gm2 = make_fx(gm)(torch.randn(2, 3))
        self.assertTrue(isinstance(gm2, _LazyGraphModule))
        self.assertTrue(gm2._needs_recompile())

        # make_fx will cal foward method of gm. That clears the _needs_recompile()
        # flag.
        self.assertFalse(gm._needs_recompile())

    def test_recapture_with_symbolic_trace(self):
        def f(x):
            return x.sin()

        gm = fx.symbolic_trace(f)
        self.assertTrue(isinstance(gm, _LazyGraphModule))
        self.assertTrue(gm._needs_recompile())
        gm2 = fx.symbolic_trace(gm)

        # the lazy recompilcation is already realized. We realize the
        # recompilation in the beginning of symbolic_trace since symbolic_trace can not
        # handle the tracing of lazy recompilation.
        self.assertFalse(gm._needs_recompile())
        self.assertTrue(gm2._needs_recompile())

    def test_recapture_with_dynamo(self):
        def f(x):
            return x.sin()

        gm = fx.symbolic_trace(f)
        self.assertTrue(isinstance(gm, _LazyGraphModule))
        self.assertTrue(gm._needs_recompile())
        torch.compile(gm)(torch.rand(2, 3))

        # dynamo calls gm.forward with eval hook installed. That will trigger
        # the real recompilation.
        self.assertFalse(gm._needs_recompile())

    def test_save_lazy_foward(self):
        """
        Save the lazy forward method and call it repeatly. Make sure we
        don't recompile for each such call.
        """

        def f(x):
            return x.sin()

        orig_gm_recompile = fx.GraphModule.recompile
        recompile_count = 0

        def mock_gm_recompile(self):
            nonlocal recompile_count
            recompile_count += 1
            return orig_gm_recompile(self)

        with patch.object(fx.GraphModule, "recompile", mock_gm_recompile):
            gm = fx.symbolic_trace(f)
            self.assertTrue(isinstance(gm, _LazyGraphModule))
            saved_fwd = gm.forward

            x = torch.rand(2, 3)
            for _ in range(10):
                saved_fwd(x)

        self.assertEqual(recompile_count, 1)

    def test_pickle(self):
        """
        Fx graph cache need the ability to pickle GraphModule/_LazyGraphModule.
        """

        def f(x):
            return x.sin()

        gm = fx.symbolic_trace(f)
        self.assertTrue(isinstance(gm, _LazyGraphModule))
        serialized = pickle.dumps(gm)
        gm2 = pickle.loads(serialized)
        self.assertTrue(isinstance(gm2, _LazyGraphModule))
        self.assertTrue("sin" in gm2.code)


if __name__ == "__main__":
    run_tests()
