# AUTOGENERATED! DO NOT EDIT! File to edit: nbs/18_callback.fp16.ipynb (unless otherwise specified).

__all__ = ['MixedPrecision', 'FP16TestCallback', 'get_master', 'to_master_grads', 'to_model_params', 'test_overflow',
           'grad_overflow', 'copy_clone', 'ModelToHalf', 'NonNativeMixedPrecision']

# Cell
from ..basics import *
from .progress import *

from torch.cuda.amp import GradScaler,autocast
from torch.cuda.amp.grad_scaler import OptState

# Cell
@delegates(GradScaler)
class MixedPrecision(Callback):
    "Mixed precision training using Pytorch's `autocast` and `GradScaler`"
    order = 10
    def __init__(self, **kwargs): self.kwargs = kwargs
    def before_fit(self):
        self.autocast,self.learn.scaler,self.scales = autocast(),GradScaler(**self.kwargs),L()
    def before_batch(self): self.autocast.__enter__()
    def after_pred(self):
        if next(flatten(self.pred)).dtype==torch.float16: self.learn.pred = to_float(self.pred)
    def after_loss(self): self.autocast.__exit__(None, None, None)
    def before_backward(self): self.learn.loss_grad = self.scaler.scale(self.loss_grad)
    def before_step(self):
        "Use `self` as a fake optimizer. `self.skipped` will be set to True `after_step` if gradients overflow. "
        self.skipped=True
        self.scaler.step(self)
        if self.skipped: raise CancelStepException()
        self.scales.append(self.scaler.get_scale())
    def after_step(self): self.learn.scaler.update()

    @property
    def param_groups(self):
        "Pretend to be an optimizer for `GradScaler`"
        return self.opt.param_groups
    def step(self, *args, **kwargs):
        "Fake optimizer step to detect whether this batch was skipped from `GradScaler`"
        self.skipped=False
    def after_fit(self): self.autocast,self.learn.scaler,self.scales = None,None,None

# Cell
class FP16TestCallback(Callback):
    "Asserts that predictions are `float16` values"
    order = 9
    def after_pred(self): assert listify(flatten(self.pred))[0].dtype==torch.float16

# Cell
@patch
@delegates(GradScaler)
def to_fp16(self:Learner, **kwargs): return self.add_cb(MixedPrecision(**kwargs))

# Cell
@patch
def to_fp32(self:Learner): return self.remove_cb(MixedPrecision)

# Cell
from ..fp16_utils import convert_network, model_grads_to_master_grads, master_params_to_model_params

# Cell
from torch.nn.utils import parameters_to_vector

# Cell
def get_master(
    opt:Optimizer, # Optimizer from which to retrieve model params
    flat_master:bool=False, # Flatten fp32 params into a vector for better performance
) -> list: # List of fp16 params, and list of fp32 params
    "Creates fp16 model params given an initialized `Optimizer`, also returning fp32 model params. "
    model_params = [[param for param in pg if getattr(param, 'requires_grad', False) and hasattr(param, 'data')] for pg in opt.param_lists]
    if flat_master:
        master_params = []
        for pg in model_params:
            mp = parameters_to_vector([param.data.float() for param in pg])
            mp = nn.Parameter(mp, requires_grad=True)
            if mp.grad is None: mp.grad = mp.new(*mp.size())
            master_params.append([mp])
    else:
        master_params = [[nn.Parameter(param.data.clone().float().detach(), requires_grad=True) for param in pg] for pg in model_params]
    return model_params, master_params

# Cell
def to_master_grads(
    model_pgs:list, # Fp16 model parameters to copy gradients from
    master_pgs:list, # Fp32 model parameters to copy gradients to
    flat_master:bool=False, # Whether or not fp32 parameters were previously flattened
):
    "Move fp16 model gradients to fp32 master gradients"
    for (model_params,master_params) in zip(model_pgs,master_pgs):
        model_grads_to_master_grads(model_params, master_params, flat_master=flat_master)

# Cell
def to_model_params(
    model_pgs:list, # Fp16 model params to copy to
    master_pgs:list, # Fp32 master params to copy from
    flat_master:bool=False # Whether master_pgs was previously flattened
)->None:
    "Copy updated fp32 master params to fp16 model params after gradient step. "
    for (model_params,master_params) in zip(model_pgs,master_pgs):
        master_params_to_model_params(model_params, master_params, flat_master=flat_master)

# Cell
def test_overflow(x:torch.Tensor):
    "Tests whether fp16 gradients have overflowed."
    s = float(x.float().sum())
    return (s == float('inf') or s == float('-inf') or s != s)

# Cell
def grad_overflow(pgs:list)->bool:
    "Tests all fp16 parameters in pgs for gradient overflow"
    for pg in pgs:
        for p in pg:
            if p.grad is not None and test_overflow(p.grad.data): return True
    return False

# Cell
def copy_clone(d):
    return {k:(v.detach().clone().float() if isinstance(v,Tensor) else v) for k,v in d.items()}

# Cell
def _copy_state(opt, pgs1, pgs2):
    opt.param_lists = pgs2
    for pg1,pg2 in zip(pgs1, pgs2):
        for p1,p2 in zip(pg1, pg2): opt.state[p2] = copy_clone(opt.state.pop(p1, {}))

# Cell
class ModelToHalf(Callback):
    "Use with NonNativeMixedPrecision callback (but it needs to run at the very beginning)"
    order=-50
    def before_fit(self): self.learn.model = convert_network(self.model, dtype=torch.float16)
    def after_fit (self): self.learn.model = convert_network(self.model, dtype=torch.float32)

# Cell
@docs
class NonNativeMixedPrecision(Callback):
    "Run training in mixed precision"
    order=10
    def __init__(self,
        loss_scale:int=512, # Non-dynamic loss scale, used to avoid underflow of gradients.
        flat_master:bool=False, # Whether to flatten fp32 parameters for performance
        dynamic:bool=True, # Whether to automatically determine loss scaling
        max_loss_scale:float=2.**24, # Starting value for dynamic loss scaling
        div_factor:float=2., # Divide by this on overflow, multiply by this after scale_wait batches
        scale_wait:int=500, # Number of batches to wait for increasing loss scale
        clip:float=None, # Value to clip gradients at, max_norm, as in `nn.utils.clip_grad_norm_`
    ):
        assert torch.backends.cudnn.enabled, "Mixed precision training requires cudnn."
        self.flat_master,self.dynamic,self.max_loss_scale = flat_master,dynamic,max_loss_scale
        self.div_factor,self.scale_wait,self.clip = div_factor,scale_wait,clip
        self.loss_scale = max_loss_scale if dynamic else loss_scale

    def before_fit(self):
        "Sets up fp16 weights for model"
        assert self.dls.device.type == 'cuda', "Mixed-precision training requires a GPU, remove the call `to_fp16`"
        if self.learn.opt is None: self.learn.create_opt()
        self.model_pgs,self.master_pgs = get_master(self.opt, self.flat_master)
        self.old_pgs = self.opt.param_lists
        #Changes the optimizer so that the optimization step is done in FP32.
        _copy_state(self.learn.opt, self.model_pgs, self.master_pgs)
        if self.dynamic: self.count = 0

    def before_batch(self): self.learn.xb = to_half(self.xb)
    def after_pred(self): self.learn.pred = to_float(self.pred)
    def before_backward(self): self.learn.loss_grad *= self.loss_scale

    def before_step(self):
        "Update and apply dynamic loss scaling, move gradients to fp32, apply gradient clipping"
        #First, check for an overflow
        if self.dynamic and grad_overflow(self.model_pgs):
            self.loss_scale /= self.div_factor
            self.learn.loss_grad /= self.div_factor #to record correct loss
            self.model.zero_grad()
            raise CancelBatchException() #skip step and zero_grad
        to_master_grads(self.model_pgs, self.master_pgs, self.flat_master)
        for master_params in self.master_pgs:
            for param in master_params:
                if param.grad is not None: param.grad.div_(self.loss_scale)
        if self.clip is not None:
            for group in self.master_pgs: nn.utils.clip_grad_norm_(group, self.clip)
        # Check if it's been long enough without overflow
        if self.dynamic:
            self.count += 1
            if self.count == self.scale_wait:
                self.count = 0
                self.loss_scale *= self.div_factor

    def after_step(self):
        "Zero grads and update fp16 params user fp32 params. "
        self.model.zero_grad() #Zero the gradients of the model manually (optimizer disconnected)
        to_model_params(self.model_pgs, self.master_pgs, self.flat_master)

    def after_batch(self):
        if self.training: self.learn.loss_grad /= self.loss_scale  #Log correct loss
    def after_fit(self):
        "Clean up fp16 specific changes to optimizer and param groups. "
        if not hasattr(self,'master_pgs'): return
        _copy_state(self.learn.opt, self.master_pgs, self.model_pgs)
        self.learn.opt.param_lists  = self.old_pgs
        delattr(self, "master_pgs")
        delattr(self, "model_pgs")
        delattr(self, "old_pgs")

    _docs = dict(before_fit="Put the model in FP16 and prepare the two copies of the parameters",
                 before_batch="Put the input in FP16",
                 after_pred="Put the output back to FP32 so that the loss is computed in FP32",
                 before_backward="Apply loss scaling to avoid gradient underflow",
                 before_step="Copy the gradients to the master param and undo the loss scaling",
                 after_step="Copy the master params to the model params",
                 after_batch="Ensure loss is logged correctly",
                 after_fit="Put the model back in FP32")

# Cell
@patch
@delegates(NonNativeMixedPrecision.__init__)
def to_non_native_fp16(self:Learner, **kwargs): return self.add_cbs([ModelToHalf(), NonNativeMixedPrecision(**kwargs)])

# Cell
@patch
def to_non_native_fp32(self: Learner): return self.remove_cbs([ModelToHalf, NonNativeMixedPrecision])