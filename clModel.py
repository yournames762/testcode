import numpy as np,copy
import torch
import torch.nn as nn
import torch.optim as optim
import torch.cuda.amp as tca
import lutils  #import learnFromExp.lutils as lutils
import math
from models import ExpNet
from torch.optim.lr_scheduler import (
    CosineAnnealingLR, LinearLR, SequentialLR
)

niter = 1e10


# def make_scheduler(optimizer, epochs,
#                    warmup_ratio=0.05,
#                    eta_min=0.0,
#                    eps=1e-8):                  #  ◀ thêm epsilon

#     warmup_epochs = max(1, int(epochs * warmup_ratio))

#     # (1) Linear warm-up: từ eps → 1.0
#     scheduler1 = LinearLR(
#         optimizer,
#         start_factor=eps,       # dùng 1e-8 thay vì 0.0
#         end_factor=1.0,
#         total_iters=warmup_epochs
#     )

#     # (2) Cosine-annealing
#     scheduler2 = CosineAnnealingLR(
#         optimizer,
#         T_max=epochs - warmup_epochs,
#         eta_min=eta_min
#     )

#     # Ghép 2 scheduler
#     return SequentialLR(
#         optimizer,
#         schedulers=[scheduler1, scheduler2],
#         milestones=[warmup_epochs]
#     )

# ---------------------------------------------------------------------
# Bổ sung cùng vị trí cũ trong clModel.py
from torch.optim.lr_scheduler import LinearLR, CosineAnnealingLR, SequentialLR

def make_scheduler(
    optimizer,
    epochs: int,
    warmup_ratio: float = 0.05,
    eta_min: float = 0.0,
    eps: float = 1e-8,
):
    """
    Linear warm-up + Cosine-annealing.
    • Nếu tổng số epoch ≤ warm-up  → chỉ dùng Linear (hoặc Constant) cho an toàn.
    • Ngược lại                 → Linear warm-up rồi đến Cosine-annealing.
    """
    # --- 1. Tính số epoch warm-up ---
    warmup_epochs = max(1, int(round(epochs * warmup_ratio)))

    # --- 2. Scheduler warm-up (từ eps → 1.0) ---
    sched_warm = LinearLR(
        optimizer,
        start_factor=eps,       # rất nhỏ nhưng > 0 nên không lỗi
        end_factor=1.0,
        total_iters=warmup_epochs,
    )

    # --- 3. Nếu còn epoch sau warm-up → Cosine; ngược lại trả luôn sched_warm ---
    if epochs > warmup_epochs:
        sched_cos = CosineAnnealingLR(
            optimizer,
            T_max=epochs - warmup_epochs,   # luôn ≥ 1 nhờ nhánh if
            eta_min=eta_min,
        )
        return SequentialLR(
            optimizer,
            schedulers=[sched_warm, sched_cos],
            milestones=[warmup_epochs],
        )
    else:
        # Trường hợp epochs == 1 : dùng LinearLR (hoặc ConstantLR) cho 1 epoch
        return sched_warm
# ---------------------------------------------------------------------


def expRun(dataset, getExp, nexp, exps,cfg,grad_cam,tarLays,norm,actModel,lrpModel,netClaDec,netclact):
    #Data consists of: X,y,all exps, estimated y of exps, logit y, normed logits
    #All Exps: List of Input Ex, Mid Ex (where Input Ex is always one entry, and Mid Ex has one list entry per layer)
    # Mid Ex for one layer: batchSize,ClassForWhichExp,splits,h,w
    # Input Ex: batchSize,ClassForWhichExp,layers,h,w
    ox, oy, expx, rawexpx, masks,aids,alogs,anlogs = [], [], [], [], [], [],[],[]
    for i, data in enumerate(dataset):
        normX = (data[0].cuda() - norm[0]) / norm[1]
        ox.append(normX.cpu().numpy().astype(np.float16))
        oy.append(data[1].numpy().astype(np.int16))
        bx, bx2,clids,logs,nlogs = lutils.batchExp(data,normX, exps, cfg, grad_cam,actModel,lrpModel,netClaDec,netclact,getExp=getExp, tarLays=tarLays)
        expx.append(bx)
        rawexpx.append(bx2)
        aids.append(clids)
        alogs.append(logs)
        anlogs.append(nlogs)
        if len(oy) * data[0].shape[0] > nexp: break
        if len(oy) % 40 == 0: print("Computed Explantions: ", len(oy) * data[0].shape[0])
    oy = np.concatenate(oy).astype(np.int16)
    ox = np.concatenate(ox, axis=0).astype(np.float16)
    ex = np.concatenate(expx, axis=0).astype(np.float16)
    aids=np.concatenate(aids,axis=0).astype(np.int16)
    alogs = np.concatenate(alogs, axis=0).astype(np.float16)
    anlogs = np.concatenate(anlogs, axis=0).astype(np.float16)
    sta = lambda i: np.concatenate([r[i] for r in rawexpx], axis=0)
    exr = [sta(i) for i in range(len(tarLays))]  #np.concatenate(rawexpx,axis=0).astype(np.float16)
    print("Total computed Explantions: ", len(oy))
    return ox, oy, [ex] + exr,aids,alogs,anlogs


def selectLays(ds, cfg): #Returns X,Y, Exp(SalMaps),classes,logits (if used later)
    tl = [0]#np.array(cfg['compExpTar']) #- 1-cfg["compExpOff"]
    ds=list(ds[:2])+ds[2]+list(ds[3:])
    return ds[:2] + [ds[3 + t] for t in tl] + [ds[-3]]

def getExps(model,cfg,train_dataset,val_dataset,norm):
    #Data consists of: X,y,all exps, estimated y of exps, logit y, normed logits
    #All Exps: List of Input Ex, Mid Ex (where Input Ex is always one entry, and Mid Ex has one list entry per layer)
    # Mid Ex for one layer: batchSize,ClassForWhichExp,splits,h,w
    # Input Ex: batchSize,ClassForWhichExp,layers,h,w
    #Exp have shape Upsampled: batchSize,#targetlays,#targetclasses,#features/splits,imgheight,imgwid
    ### Exp have shape for nonexp: batchSize,#targetclasses,#targetlays,#splits,imgheight,imgwid
    #modcfg=copy.deepcopy(cfg)    #ecfg = modcfg["clcfgForExp"]  modcfg["clcfg"]=ecfg
    #model, lcfg, loadedExp = getclassifier(modcfg,  train_dataset, val_dataset, None,forceLoad=True)  # if "trainCl" in cfg: return
    grad_cam, actModel, lrpModel,netClaDec,netclact = None, None, None,None,None
    grad_cam=lutils.getGradcam(cfg,model,cfg["compExpTar"])
    print("Compute Explanations for training data")
    d = expRun(train_dataset,True,cfg["ntrain"],cfg["exps"],cfg,grad_cam,cfg["compExpTar"],norm,actModel,lrpModel,netClaDec,netclact)#"CMSTR"
    print("Compute Explanations for testing data")
    vd = expRun(val_dataset, True, cfg["ntrain"] // 2, cfg["exps"],cfg,grad_cam,cfg["compExpTar"],norm,actModel,lrpModel,netClaDec,netclact) #"CMSTRRRRRR"
    return d,vd

def decay(opt,epoch,optimizerCl):
    if opt[0] == "S" and (epoch + 1) % (opt[1] // 3+opt[1]//10+2 ) == 0:
        for p in optimizerCl.param_groups: p['lr'] *= 0.1
        #print("  D", np.round(optimizerCl.param_groups[0]['lr'],5))

def getSingleAcc(net, dsx, labels, pool=None):
  with tca.autocast():
    outputs = net(dsx)
    if type(outputs) is tuple: outputs=outputs[1] #for attention net use second output
    _, predicted = torch.max(outputs.data, 1)
    correct = torch.eq(predicted,labels).sum().item()
    return correct

def getEAcc(net, dataset, iexp,  niter=10000, pool=None, zeroExp=1, cfg=None):
    correct,total = 0,0
    net.eval()
    with torch.no_grad():
        for i,data in enumerate(dataset):
            labels = data[1].cuda()
            edat=[d.clone().numpy() for d in data[2:]]
            nd,_=getexp(edat, cfg, iexp, zeroExp=zeroExp, isTrain=False)
            xgpu=data[0].cuda()
            ndgpu=[torch.from_numpy(x).cuda() for x in nd]
            correct += getSingleAcc(net, (xgpu, ndgpu), labels, pool=pool)
            total += labels.size(0)
            if i>=niter: break
    return correct/total


def getAcc(net, dataset,  niter=10000,norm=None):
    correct,total = 0,0
    net.eval()
    with torch.no_grad():
        for cit,data in enumerate(dataset):
            with tca.autocast():
                dsx,dsy = data[0].cuda(),data[1].cuda()
                dsx = (dsx - norm[0])/norm[1]
                total += dsy.size(0)
                outputs = net(dsx.float())
                _, predicted = torch.max(outputs.data, 1)
                correct += torch.eq(predicted, dsy).sum().item()
                if cit>=niter: break
    return correct/total

def getexp(data,cfg,ranOpt,zeroExp,isTrain=True):
    #Mid Ex for one layer:
    #Return for Mid Ex: List of Exp: one entry for each layer (+ if aexp one entry containing all classes -> this works only for nin==1)
    #One entry per layer: batchSize,ClassForWhichExp (==1 for nin=1),splits,h,w
    inds=None
    if isTrain:
        inds = np.random.choice(ranOpt, data[0].shape[0])  # expx = np.copy(data)[:, :1] for d in data: print(d.shape,"ds",ranOpt)
        lex = [np.expand_dims(d[np.arange(d.shape[0]), inds], axis=1) for d in data]
    else:
        lex = [np.expand_dims(d[:, ranOpt[0]], axis=1) for d in data]
    return lex,inds

def getxdat(xdat,zeroExp,aug,cfg,ranOpt):
    rdat = [x.clone().numpy() for x in xdat[1:]] #Selected explanations - don't change original input at 0
    ex,inds = getexp(rdat, cfg, ranOpt, zeroExp=zeroExp)
    return [xdat[0].cuda()]+[torch.from_numpy(d).cuda() for d in ex],inds

def getOut(ndgpu,netCl,cfg):
    dropCl = False
    dropA =  False
    output = netCl((ndgpu[0], ndgpu[1:],dropCl,dropA))
    return output

def getNet(cfg,ccf,isExp):
    NETWORK = ExpNet
    netCl = NETWORK(cfg, cfg["num_classes"],isExp).cuda()
    return netCl

def getExpClassifier(cfg,  train_dataset, val_dataset, resFolder, trainedNetSelf=None): #"Train Reflective Classifier"
    netCl=getNet(cfg,None,True)
    aep,asp= list(netCl.parameters()), trainedNetSelf.parameters()
    for iep,sp in enumerate(asp):
        ep=aep[iep]
        if sum(list(sp.data.shape))!=sum(list(ep.data.shape)):
            if len(sp.shape)>1: ep.data[:sp.data.shape[0],:sp.data.shape[1]]=sp.data.clone()
            else: ep.data[:sp.data.shape[0]]=sp.data.clone()
        else: ep.data.copy_(sp.data)
    optimizerCl = optim.SGD(netCl.parameters(), lr=cfg["opt"][2], momentum=0.9, weight_decay=cfg["opt"][3]) #elif ccf["opt"][0] == "A": optimizerCl = optim.Adam(netCl.parameters(), ccf["opt"][2], weight_decay=ccf["opt"][3])
    closs, trep, loss = 0,  cfg["opt"][1], nn.CrossEntropyLoss()
    print("Train Reflective Classifier")
    scaler = tca.GradScaler()
    ranOpt=np.sort(np.array([0,1])) #sorting is very important
    emateAccs,etrAccs=[],[]
    iexp = list(np.arange(len(cfg["exps"])))
    icorr=iexp[:1]+iexp[2:]
    imax=iexp[2:]
    for epoch in range(trep):
        netCl.train()
        for i, data in enumerate(train_dataset):
            with tca.autocast():
                optimizerCl.zero_grad()
                dsy = data[1].cuda()
                ndgpu,inds = getxdat([data[0]] + list(data[2:]),False,False,cfg,ranOpt)
                output=getOut(ndgpu,netCl,cfg)
                errD_real = loss(output, dsy.long())
                scaler.scale(errD_real).backward()
                scaler.step(optimizerCl)
                scaler.update()
                closs = 0.97 * closs + 0.03 * errD_real.item() if epoch > 20 else 0.8 * closs + 0.2 * errD_real.item()
        decay(cfg["opt"],epoch,optimizerCl)
        netCl.eval()
        emateAccs.append(getEAcc(netCl, val_dataset, imax,  niter=niter, cfg=cfg) if len(imax) else -1)
        #etrAccs.append(getEAcc(netCl, train_dataset, imax,  niter=niter, cfg=cfg) if len(imax) else -1)
        if (epoch % 4 == 0 and epoch<=13) or (epoch % 20==0 and epoch>13) :
            cacc=getEAcc(netCl, val_dataset, iexp[1:],  niter=niter, cfg=cfg)
            print(epoch, np.round(np.array([closs, cacc, getEAcc(netCl, train_dataset, icorr,  niter=niter, cfg=cfg)]), 5))
            if np.isnan(closs):
                print("Failed!!!")
                return None,None
    netCl.eval()
    lcfg = {"testAccCorr": getEAcc(netCl, val_dataset, [0],  niter=niter, cfg=cfg),
                "testAccPred": getEAcc(netCl, val_dataset, [2] ,  niter=niter, cfg=cfg) if len(iexp)>2 else -1,
                "testAccRan": getEAcc(netCl, val_dataset, [1],  niter=niter, cfg=cfg)}
                #"teAccsEMa": emateAccs,"trAccsEMa": etrAccs}
    setEval(netCl)
    return netCl, lcfg

def setEval(netCl):
        netCl.eval()
        for name, module in netCl.named_modules():
            if isinstance(module, nn.Dropout): module.p = 0
            elif isinstance(module, nn.LSTM): module.dropout = 0 #print("zero lstm drop") #print("zero drop")
            elif isinstance(module, nn.GRU): module.dropout = 0

def getLo(model):
    reg_loss = 0
    for name,param in model.named_parameters():
        if 'bn' not in name:
             reg_loss += torch.norm(param)
    #loss = cls_loss + args.weight_decay*reg_loss
    return reg_loss



# ===== clModel.py =====
import math
from torch.optim.lr_scheduler import (
    CosineAnnealingLR, LinearLR, SequentialLR
)

def make_scheduler(optimizer, epochs, warmup_ratio=0.05, eta_min=0.0):
    """
    Trả về LR-scheduler Cosine với pha warm-up tuyến tính (LinearLR).

    Parameters
    ----------
    optimizer : torch.optim.Optimizer
    epochs    : int  – tổng số epoch train
    warmup_ratio : float – tỉ lệ warm-up (0.05 = 5 %)
    eta_min   : float – learning-rate tối thiểu cuối training
    """
    warmup_epochs = max(1, int(epochs * warmup_ratio))
    # 1) Linear warm-up từ 0 → base_lr
    scheduler1 = LinearLR(
        optimizer,
        start_factor=1e-8,
        end_factor=1.0,
        total_iters=warmup_epochs
    )
    # 2) Cosine-annealing từ base_lr → eta_min
    scheduler2 = CosineAnnealingLR(
        optimizer,
        T_max=epochs - warmup_epochs,
        eta_min=eta_min
    )
    # Ghép 2 scheduler nối tiếp
    return SequentialLR(
        optimizer,
        schedulers=[scheduler1, scheduler2],
        milestones=[warmup_epochs]
    )




# --------------------------------------------------------------------


#####CHINH SUA


# --------------------------------------------------------------------
def getclassifier(cfg, train_dataset, val_dataset,
                  resFolder, forceLoad=False, norm=None):
    """Huấn luyện classifier baseline với Cosine-LR + warm-up."""
    netCl = getNet(cfg, None, False).cuda()
    optimizerCl = optim.SGD(
        netCl.parameters(), lr=cfg["opt"][2],
        momentum=0.9, weight_decay=cfg["opt"][3]
    )
    # <── tạo scheduler mới ──>
    epochs = cfg["opt"][1]
    scheduler = make_scheduler(
        optimizerCl, epochs,
        warmup_ratio=0.05,          # =5 % epoch đầu
        eta_min=cfg["opt"][2] * 1e-4
    )

    scaler = tca.GradScaler()
    loss_fn = nn.CrossEntropyLoss()
    closs, teAccs = 0.0, []
    clAcc = lambda loader: getAcc(netCl, loader, niter=niter, norm=norm)

    print("Train non-reflective classifier  (Cosine + warm-up)")
    for epoch in range(epochs):
        netCl.train()
        for i, (x, y) in enumerate(train_dataset):
            optimizerCl.zero_grad(set_to_none=True)
            with tca.autocast():
                x = (x.cuda() - norm[0]) / norm[1]
                y = y.cuda()
                out   = netCl(x.float())
                err   = loss_fn(out, y.long())
            scaler.scale(err).backward()
            scaler.step(optimizerCl)
            scaler.update()

            # EMA loss mượt hơn
            alpha = 0.97 if i > 20 else 0.8
            closs = alpha * closs + (1-alpha) * err.item()

        scheduler.step()                     #  ◀── cập nhật LR
        netCl.eval()
        teAccs.append(clAcc(val_dataset))

        if (epoch < 14 and epoch % 4 == 0) or (epoch % 20 == 0):
            print(f"{epoch:3d}",
                  np.round([closs,
                            teAccs[-1],
                            clAcc(train_dataset),
                            max(teAccs)], 5))

        if math.isnan(closs):
            raise RuntimeError("Loss exploded to NaN!")

    lcfg = {"testAcc": teAccs[-1], "trainAcc": clAcc(train_dataset)}
    setEval(netCl)
    return netCl, lcfg, False

# def getclassifier(cfg,train_dataset,val_dataset,resFolder,forceLoad=False,norm=None):
#     netCl=getNet(cfg,None,False)
#     optimizerCl = optim.SGD(netCl.parameters(), lr=cfg["opt"][2], momentum=0.9, weight_decay=cfg["opt"][3])
#     closs,teaccs,trep,loss,clr = 0,[],cfg["opt"][1],nn.CrossEntropyLoss(), cfg["opt"][2]
#     '''
#     trep = cfg["opt"][1]  (Số epoch)
#     closs = 0 ->  exponential moving average của loss để in mượt
#     '''
#     print("Train non-reflective classifier")
#     scaler = tca.GradScaler()   
#     teAccs,trAccs=[],[] #Lịch sử độ chính xác
#     clAcc = lambda dataset: getAcc(netCl, dataset,  niter=niter,norm=norm) #Hàm đo Accuracy trên Loader đã chuẩn hoá
#     for epoch in range(trep):
#         netCl.train()
#         for i, data in enumerate(train_dataset):
#           with tca.autocast():
#             optimizerCl.zero_grad()
#             dsx = data[0]
#             dsx,dsy = dsx.cuda(),data[1].cuda()
#             dsx=(dsx-norm[0])/norm[1]
#             output = netCl(dsx.float())  # if useAtt:                #     errD_real = loss(output[0], dsy.long())+loss(output[1], dsy.long())                #     output=output[1] #prediction outputs                # else:
#             errD_real = loss(output, dsy.long())
#             scaler.scale(errD_real).backward()
#             scaler.step(optimizerCl)
#             scaler.update()
#             closs = 0.97 * closs + 0.03 * errD_real.item() if i > 20 else 0.8 * closs + 0.2 * errD_real.item()
#         decay(cfg["opt"],epoch,optimizerCl)
#         netCl.eval()
#         teAccs.append(clAcc(val_dataset))
#         #trAccs.append(clAcc(train_dataset))
#         if (epoch % 4 == 0 and epoch<=13) or (epoch % 20==0 and epoch>13):
#             print(epoch, np.round(np.array([closs, teAccs[-1], clAcc(train_dataset),max(teAccs)]), 5))
#             if np.isnan(closs):
#                 print("Failed!!!")
#                 return None,None
#     lcfg = {"testAcc": clAcc(val_dataset), "trainAcc": clAcc(train_dataset)}
#     setEval(netCl)
#     return netCl, lcfg,False