import sys
import os
import copy
import time
from collections import namedtuple
from functools import partial
import numpy as np
from lmfit import Parameters, minimize, fit_report
from matplotlib.ticker import FuncFormatter

import wx
from wxmplot import PlotPanel
from xraydb import guess_edge
from larch import Group, isgroup
from larch.math import index_of, index_nearest, interp
from larch.utils.strutils import file2groupname, unique_name
from larch.io import unixpath

from larch.wxlib import (GridPanel, BitmapButton, FloatCtrl, FloatSpin,
                         set_color, FloatSpinWithPin, get_icon, SimpleText,
                         Choice, SetTip, Check, Button, HLine, OkCancel,
                         LEFT, pack, plotlabels, ReportFrame, DictFrame,
                         FileCheckList, Font, FONTSIZE, plotlabels,
                         get_zoomlimits, set_zoomlimits)

from larch.xafs import etok, ktoe, find_energy_step
from larch.utils.physical_constants import PI, DEG2RAD, PLANCK_HC, ATOM_SYMS
from larch.math import smooth

Plot_Choices = {'Normalized': 'norm', 'Derivative': 'dmude'}


EDGE_LIST = ('K', 'L3', 'L2', 'L1', 'M5', 'M4', 'M3')

NORM_MU = 'Normalized \u03BC(E)'

DEGLITCH_PLOTS = {'Raw \u03BC(E)': 'mu',
                  NORM_MU: 'norm',
                  '\u03c7(E)': 'chie',
                  '\u03c7(E)*(E-E_0)': 'chiew'}

SESSION_PLOTS = {'Normalized \u03BC(E)': 'norm',
                 'Raw \u03BC(E)': 'mu',
                 'k^2\u03c7(k)': 'chikw'}


def ensure_en_orig(dgroup):
    if not hasattr(dgroup, 'energy_orig'):
        dgroup.energy_orig = dgroup.energy[:]



def fit_dialog_window(dialog, panel):
    sizer = wx.BoxSizer(wx.VERTICAL)
    sizer.Add(panel, 1, LEFT, 5)
    pack(dialog, sizer)
    dialog.Fit()
    w0, h0 = dialog.GetSize()
    w1, h1 = dialog.GetBestSize()
    dialog.SetSize((max(w0, w1)+25, max(h0, h1)+25))



def add_floatspin(name, value, panel, with_pin=True, xasmain=None,
                  callback=None, relative_e0=False, **kws):
    """create FloatSpin with Pin button for onSelPoint"""
    if with_pin and xasmain is not None:
        pin_action = partial(xasmain.onSelPoint, opt=name,
                             relative_e0=relative_e0,
                             callback=callback)
        fspin, pinb = FloatSpinWithPin(panel, value=value,
                                       pin_action=pin_action, **kws)
    else:
        fspin = FloatSpin(panel, value=value, **kws)
        pinb = None
    return fspin, pinb


class OverAbsorptionDialog(wx.Dialog):
    """dialog for correcting over-absorption"""
    def __init__(self, parent, controller, **kws):
        self.parent = parent
        self.controller = controller
        self.dgroup = self.controller.get_group()
        groupnames = list(self.controller.file_groups.keys())

        self.data = [self.dgroup.energy[:], self.dgroup.norm[:]]

        wx.Dialog.__init__(self, parent, wx.ID_ANY, size=(550, 400),
                           title="Correct Over-absorption")
        self.SetFont(Font(FONTSIZE))
        panel = GridPanel(self, ncols=3, nrows=4, pad=4, itemstyle=LEFT)
        self.wids = wids = {}

        wids['grouplist'] = Choice(panel, choices=groupnames, size=(250, -1),
                                   action=self.on_groupchoice)

        wids['grouplist'].SetStringSelection(self.dgroup.filename)

        opts  = dict(size=(90, -1), precision=1, act_on_losefocus=True,
                     minval=-90, maxval=180)

        fs_opts = dict(size=(90, -1), value=45, digits=1, increment=1)
        wids['phi_in']  = FloatSpin(panel, **fs_opts)
        wids['phi_out'] = FloatSpin(panel, **fs_opts)

        wids['elem'] = Choice(panel, choices=ATOM_SYMS[:98], size=(50, -1))
        wids['edge'] = Choice(panel, choices=EDGE_LIST, size=(50, -1))

        wids['formula'] = wx.TextCtrl(panel, -1, '', size=(250, -1))

        self.set_default_elem_edge(self.dgroup)

        #wids['apply'] = Button(panel, 'Save / Overwrite', size=(150, -1),
        #                       action=self.on_apply)
        #SetTip(wids['apply'], 'Save corrected data, overwrite current arrays')

        wids['save_as'] = Button(panel, 'Save As New Group: ', size=(150, -1),
                           action=self.on_saveas)
        SetTip(wids['save_as'], 'Save corrected data as new group')

        wids['save_as_name'] = wx.TextCtrl(panel, -1, self.dgroup.filename + '_abscorr',
                                           size=(250, -1))
        wids['correct'] = Button(panel, 'Do Correction',
                                 size=(150, -1), action=self.on_correct)
        SetTip(wids['correct'], 'Calculate Correction')

        def add_text(text, dcol=1, newrow=True):
            panel.Add(SimpleText(panel, text), dcol=dcol, newrow=newrow)

        add_text(' Correction for Group: ', newrow=False)
        panel.Add(wids['grouplist'], dcol=5)

        add_text(' Absorbing Element: ')
        panel.Add(wids['elem'])

        add_text('  Edge:  ', newrow=False)
        panel.Add(wids['edge'])

        add_text(' Material Formula: ')
        panel.Add(wids['formula'], dcol=3)

        add_text(' Incident Angle (deg): ')
        panel.Add(wids['phi_in'])

        add_text(' Exit Angle (deg): ')
        panel.Add(wids['phi_out'])

        panel.Add(wids['correct'], newrow=True)
        # panel.Add(wids['apply'], dcol=2, newrow=True)

        panel.Add(wids['save_as'], newrow=True)
        panel.Add(wids['save_as_name'], dcol=3)
        panel.Add(Button(panel, 'Done', size=(150, -1), action=self.onDone),
                  newrow=True)
        panel.pack()
        fit_dialog_window(self, panel)


    def onDone(self, event=None):
        self.Destroy()

    def set_default_elem_edge(self, dgroup):
        elem, edge = guess_edge(dgroup.e0)
        self.wids['elem'].SetStringSelection(elem)
        self.wids['edge'].SetStringSelection(edge)

    def on_groupchoice(self, event=None):
        fname = self.wids['grouplist'].GetStringSelection()
        self.dgroup = self.controller.get_group(fname)
        self.set_default_elem_edge(self.dgroup)
        self.wids['save_as_name'].SetValue(self.dgroup.filename + '_abscorr')


    def on_correct(self, event=None):
        wids = self.wids
        dgroup = self.dgroup
        anginp = wids['phi_in'].GetValue()
        angout = wids['phi_out'].GetValue()
        elem   = wids['elem'].GetStringSelection()
        edge   = wids['edge'].GetStringSelection()
        formula = wids['formula'].GetValue()
        if len(formula) < 1:
            return

        cmd = """fluo_corr(%s.energy, %s.mu, '%s', '%s', edge='%s', group=%s,
     anginp=%.1f, angout=%.1f)""" % (dgroup.groupname, dgroup.groupname,
                                     formula, elem, edge, dgroup.groupname,
                                     anginp, angout)
        self.cmd = cmd
        self.controller.larch.eval(cmd)
        self.plot_results()

    def on_apply(self, event=None):
        xdat, ydat = self.data
        dgroup = self.dgroup
        dgroup.xdat = dgroup.energy = xdat
        self.parent.process_normalization(dgroup)
        dgroup.journal.add('fluor_corr_command', self.cmd)
        self.plot_results()

    def on_saveas(self, event=None):
        wids = self.wids
        fname = self.wids['grouplist'].GetStringSelection()
        new_fname = wids['save_as_name'].GetValue()
        ngroup = self.controller.copy_group(fname, new_filename=new_fname)

        if hasattr(self.dgroup, 'norm_corr' ):
            ngroup.mu = ngroup.norm_corr*1.0
            del ngroup.norm_corr

        ogroup = self.controller.get_group(fname)
        self.parent.install_group(ngroup, journal=ogroup.journal)
        olddesc = ogroup.journal.get('source_desc').value
        ngroup.journal.add('source_desc', f"fluo_corrected({olddesc})")
        ngroup.journal.add('fluor_correction_command', self.cmd)

    def plot_results(self, event=None):
        ppanel = self.controller.get_display(stacked=False).panel
        ppanel.oplot
        dgroup = self.dgroup
        zoom_limits = get_zoomlimits(ppanel, dgroup)
        path, fname = os.path.split(dgroup.filename)

        opts = dict(linewidth=3, ylabel=plotlabels.norm,
                    xlabel=plotlabels.energy, delay_draw=True,
                    show_legend=True)

        if self.controller.plot_erange is not None:
            opts['xmin'] = dgroup.e0 + self.controller.plot_erange[0]
            opts['xmax'] = dgroup.e0 + self.controller.plot_erange[1]

        ppanel.plot(dgroup.energy, dgroup.norm_corr, zorder=10, marker=None,
                    title='Over-absorption Correction:\n %s' % fname,
                    label='corrected', **opts)

        ppanel.oplot(dgroup.energy, dgroup.norm, zorder=10, marker='o',
                     markersize=3, label='original', **opts)
        set_zoomlimits(ppanel, zoom_limits) or ppanel.unzoom_all()
        ppanel.canvas.draw()
        ppanel.conf.draw_legend(show=True)

    def GetResponse(self):
        raise AttributeError("use as non-modal dialog!")


class EnergyCalibrateDialog(wx.Dialog):
    """dialog for calibrating energy"""
    def __init__(self, parent, controller, **kws):

        self.parent = parent
        self.controller = controller
        self.dgroup = self.controller.get_group()
        groupnames = list(self.controller.file_groups.keys())

        ensure_en_orig(self.dgroup)

        self.data = [self.dgroup.energy_orig[:], self.dgroup.norm[:]]
        xmin = min(self.dgroup.energy_orig)
        xmax = max(self.dgroup.energy_orig)
        e0val = getattr(self.dgroup, 'e0', xmin)

        wx.Dialog.__init__(self, parent, wx.ID_ANY, size=(550, 400),
                           title="Calibrate / Align Energy")
        self.SetFont(Font(FONTSIZE))
        panel = GridPanel(self, ncols=3, nrows=4, pad=4, itemstyle=LEFT)

        self.wids = wids = {}
        wids['grouplist'] = Choice(panel, choices=groupnames, size=(275, -1),
                                   action=self.on_groupchoice)
        wids['grouplist'].SetStringSelection(self.dgroup.filename)

        refgroups = ['None'] + groupnames
        wids['reflist'] = Choice(panel, choices=refgroups, size=(275, -1),
                                 action=self.on_align, default=0)

        opts = dict(size=(90, -1), digits=3, increment=0.1)

        opts['action'] = partial(self.on_calib, name='eshift')
        wids['eshift'] = FloatSpin(panel, value=0, **opts)

        self.plottype = Choice(panel, choices=list(Plot_Choices.keys()),
                                   size=(275, -1), action=self.plot_results)
        wids['do_align'] = Button(panel, 'Auto Align', size=(100, -1),
                                  action=self.on_align)

        wids['apply_one'] = Button(panel, 'Apply to Current Group', size=(200, -1),
                                   action=self.on_apply_one)

        wids['apply_sel'] = Button(panel, 'Apply to Selected Groups',
                                   size=(250, -1),  action=self.on_apply_sel)
        SetTip(wids['apply_sel'], 'Apply the Energy Shift to all Selected Groups')

        wids['save_as'] = Button(panel, 'Save As New Group ', size=(200, -1),
                           action=self.on_saveas)
        SetTip(wids['save_as'], 'Save shifted data as new group')

        wids['save_as_name'] = wx.TextCtrl(panel, -1,
                                           self.dgroup.filename + '_eshift',
                                           size=(275, -1))

        wids['sharedref_msg'] = wx.StaticText(panel, label="1 groups share this energy reference")
        wids['select_sharedref'] = Button(panel, 'Select Groups with shared reference',
                                          size=(300, -1),  action=self.on_select_sharedrefs)

        def add_text(text, dcol=1, newrow=True):
            panel.Add(SimpleText(panel, text), dcol=dcol, newrow=newrow)

        add_text(' Current Group: ',  newrow=False)
        panel.Add(wids['grouplist'], dcol=2)

        add_text(' Auto-Align to : ')
        panel.Add(wids['reflist'], dcol=2)

        add_text(' Plot Arrays as: ')
        panel.Add(self.plottype, dcol=2)

        add_text(' Energy Shift (eV): ')
        panel.Add(wids['eshift'], dcol=1)
        panel.Add(wids['do_align'], dcol=1)
        panel.Add(HLine(panel, size=(500, 3)), dcol=4, newrow=True)
        # panel.Add(apply_one, newrow=True)

        panel.Add(wids['sharedref_msg'], dcol=2, newrow=True)
        panel.Add(wids['select_sharedref'], dcol=2)
        panel.Add(wids['apply_one'], dcol=1, newrow=True)
        panel.Add(wids['apply_sel'], dcol=2)

        panel.Add(HLine(panel, size=(500, 3)), dcol=4, newrow=True)

        panel.Add(wids['save_as'], newrow=True)
        panel.Add(wids['save_as_name'], dcol=3)

        panel.pack()

        fit_dialog_window(self, panel)

        self.plot_results()
        wx.CallAfter(self.get_groups_shared_energyrefs)

    def on_select(self, event=None, opt=None):
        _x, _y = self.controller.get_cursor()
        if opt in self.wids:
            self.wids[opt].SetValue(_x)

    def get_groups_shared_energyrefs(self, dgroup=None):
        if dgroup is None:
            dgroup = self.controller.get_group(self.wids['grouplist'].GetStringSelection())
        sharedrefs = [dgroup.filename]
        try:
            eref = dgroup.config.xasnorm.get('energy_ref', None)
        except:
            eref = None
        if eref is None:
            eref = dgroup.groupname


        for key, val in self.controller.file_groups.items():
            if dgroup.groupname == val:
                continue
            g = self.controller.get_group(val)
            try:
                geref = g.config.xasnorm.get('energy_ref', None)
            except:
                geref = None
            # print(key, val, geref, geref == ref_filename)
            if geref == eref or geref == dgroup.filename:
                sharedrefs.append(key)
        self.wids['sharedref_msg'].SetLabel(f" {len(sharedrefs):d} groups share this energy reference")
        return sharedrefs

    def on_select_sharedrefs(self, event=None):
        groups = self.get_groups_shared_energyrefs()
        self.controller.filelist.SetCheckedStrings(groups)

    def on_groupchoice(self, event=None):
        dgroup = self.controller.get_group(self.wids['grouplist'].GetStringSelection())
        self.dgroup = dgroup
        others = self.get_groups_shared_energyrefs(dgroup)
        self.wids['save_as_name'].SetValue(self.dgroup.filename + '_eshift')
        self.plot_results()

    def on_align(self, event=None, name=None, value=None):
        ref = self.controller.get_group(self.wids['reflist'].GetStringSelection())
        dat = self.dgroup
        ensure_en_orig(dat)
        ensure_en_orig(ref)

        dat.xdat = dat.energy_orig[:]
        ref.xdat = ref.energy_orig[:]
        estep = find_energy_step(dat.xdat)
        i1 = index_of(ref.energy_orig, ref.e0-20)
        i2 = index_of(ref.energy_orig, ref.e0+20)

        def resid(pars, ref, dat, i1, i2):
            "fit residual"
            newx = dat.xdat + pars['eshift'].value
            scale = pars['scale'].value
            y = interp(newx, dat.dmude, ref.xdat, kind='cubic')
            return smooth(ref.xdat, y*scale-ref.dmude, xstep=estep, sigma=0.50)[i1:i2]

        params = Parameters()
        params.add('eshift', value=ref.e0-dat.e0, min=-50, max=50)
        params.add('scale', value=1, min=0, max=50)

        result = minimize(resid, params, args=(ref, dat, i1, i2))
        eshift = result.params['eshift'].value
        self.wids['eshift'].SetValue(eshift)

        ensure_en_orig(self.dgroup)
        xnew = self.dgroup.energy_orig + eshift
        self.data = xnew, self.dgroup.norm[:]
        self.plot_results()

    def on_calib(self, event=None, name=None):
        wids = self.wids
        eshift = wids['eshift'].GetValue()
        ensure_en_orig(self.dgroup)
        xnew = self.dgroup.energy_orig + eshift
        self.data = xnew, self.dgroup.norm[:]
        self.plot_results()

    def on_apply_one(self, event=None):
        xdat, ydat = self.data
        dgroup = self.dgroup
        eshift = self.wids['eshift'].GetValue()

        ensure_en_orig(dgroup)

        idx, norm_page = self.parent.get_nbpage('norm')
        norm_page.wids['energy_shift'].SetValue(eshift)

        dgroup.energy_shift = eshift
        dgroup.xdat = dgroup.energy = eshift + dgroup.energy_orig[:]
        dgroup.journal.add('energy_shift ', eshift)
        self.parent.process_normalization(dgroup)
        self.plot_results()

    def on_apply_sel(self, event=None):
        eshift = self.wids['eshift'].GetValue()
        idx, norm_page = self.parent.get_nbpage('norm')
        for checked in self.controller.filelist.GetCheckedStrings():
            fname  = self.controller.file_groups[str(checked)]
            dgroup = self.controller.get_group(fname)
            ensure_en_orig(dgroup)
            dgroup.energy_shift = eshift
            norm_page.wids['energy_shift'].SetValue(eshift)

            dgroup.xdat = dgroup.energy = eshift + dgroup.energy_orig[:]
            dgroup.journal.add('energy_shift ', eshift)
            self.parent.process_normalization(dgroup)

    def on_saveas(self, event=None):
        wids = self.wids
        fname = wids['grouplist'].GetStringSelection()
        eshift = wids['eshift'].GetValue()
        new_fname = wids['save_as_name'].GetValue()
        ngroup = self.controller.copy_group(fname, new_filename=new_fname)

        ensure_en_orig(ngroup)
        ngroup.xdat = ngroup.energy = eshift + ngroup.energy_orig[:]
        ngroup.energy_shift = 0
        ngroup.energy_ref = ngroup.groupname

        ogroup = self.controller.get_group(fname)
        self.parent.install_group(ngroup, journal=ogroup.journal)
        olddesc = ogroup.journal.get('source_desc').value
        ngroup.journal.add('source_desc', f"energy_shifted({olddesc}, {eshift:.4f})")
        ngroup.journal.add('energy_shift ', 0.0)

    def plot_results(self, event=None):
        ppanel = self.controller.get_display(stacked=False).panel
        ppanel.oplot
        xnew, ynew = self.data
        dgroup = self.dgroup
        zoom_limits = get_zoomlimits(ppanel, dgroup)
        path, fname = os.path.split(dgroup.filename)

        wids = self.wids
        eshift = wids['eshift'].GetValue()
        e0_old = dgroup.e0
        e0_new = dgroup.e0 + eshift

        xmin = min(e0_old, e0_new) - 25
        xmax = max(e0_old, e0_new) + 50

        use_deriv = self.plottype.GetStringSelection().lower().startswith('deriv')

        ylabel = plotlabels.norm
        if use_deriv:
            ynew = np.gradient(ynew)/np.gradient(xnew)
            ylabel = plotlabels.dmude

        opts = dict(xmin=xmin, xmax=xmax, ylabel=ylabel,
                    xlabel=plotlabels.energy, show_legend=True)

        if self.controller.plot_erange is not None:
            opts['xmin'] = dgroup.e0 + self.controller.plot_erange[0]
            opts['xmax'] = dgroup.e0 + self.controller.plot_erange[1]

        xold, yold = self.dgroup.energy_orig, self.dgroup.norm
        if use_deriv:
            yold = np.gradient(yold)/np.gradient(xold)

        ppanel.plot(xold, yold, zorder=10, marker='o', markersize=3,
                     label='original', linewidth=2, color='#1f77b4',
                     title=f'Energy Calibration:\n {fname}',
                     delay_draw=True, **opts)

        ppanel.oplot(xnew, ynew, zorder=15, marker='+', markersize=3,
                    linewidth=2, label='shifted', delay_draw=True,
                    color='#d62728', **opts)

        if wids['reflist'].GetStringSelection() != 'None':
            refgroup = self.controller.get_group(wids['reflist'].GetStringSelection())
            xref, yref = refgroup.energy, refgroup.norm
            if use_deriv:
                yref = np.gradient(yref)/np.gradient(xref)

            ppanel.oplot(xref, yref, style='solid', zorder=5, color='#2ca02c',
                         marker=None, label=refgroup.filename, **opts)

        set_zoomlimits(ppanel, zoom_limits) or ppanel.unzoom_all()
        ppanel.canvas.draw()



    def GetResponse(self):
        raise AttributeError("use as non-modal dialog!")

class RebinDataDialog(wx.Dialog):
    """dialog for rebinning data to standard XAFS grid"""
    def __init__(self, parent, controller, **kws):

        self.parent = parent
        self.controller = controller
        self.dgroup = self.controller.get_group()
        groupnames = list(self.controller.file_groups.keys())

        xmin = min(self.dgroup.energy)
        xmax = max(self.dgroup.energy)
        e0val = getattr(self.dgroup, 'e0', xmin)
        xanes_step = 0.05 * (1 + max(1, int(e0val / 2000.0)))
        self.data = [self.dgroup.energy[:], self.dgroup.mu[:],
                     self.dgroup.mu*0, e0val]

        wx.Dialog.__init__(self, parent, wx.ID_ANY, size=(550, 400),
                           title="Rebin mu(E) Data")
        self.SetFont(Font(FONTSIZE))
        panel = GridPanel(self, ncols=3, nrows=4, pad=2, itemstyle=LEFT)

        self.wids = wids = {}

        wids['grouplist'] = Choice(panel, choices=groupnames, size=(250, -1),
                                action=self.on_groupchoice)

        wids['grouplist'].SetStringSelection(self.dgroup.groupname)

        opts  = dict(size=(90, -1), precision=3, act_on_losefocus=True)

        wids['e0'] = FloatCtrl(panel, value=e0val, minval=xmin, maxval=xmax, **opts)
        pre1 = 10.0*(1+int((xmin-e0val)/10.0))
        wids['pre1'] = FloatCtrl(panel, value=pre1,  **opts)
        wids['pre2'] = FloatCtrl(panel, value=-15, **opts)
        wids['xanes1'] = FloatCtrl(panel, value=-15,  **opts)
        wids['xanes2'] = FloatCtrl(panel, value=15, **opts)
        wids['exafs1'] = FloatCtrl(panel, value=etok(15),  **opts)
        wids['exafs2'] = FloatCtrl(panel, value=etok(xmax-e0val), **opts)

        wids['pre_step'] = FloatCtrl(panel, value=2.0,  **opts)
        wids['xanes_step'] = FloatCtrl(panel, value=xanes_step,  **opts)
        wids['exafs_step'] = FloatCtrl(panel, value=0.05,  **opts)

        for wname, wid in wids.items():
            if wname != 'grouplist':
                wid.SetAction(partial(self.on_rebin, name=wname))

        #wids['apply'] = Button(panel, 'Save / Overwrite', size=(150, -1),
        #                       action=self.on_apply)
        #SetTip(wids['apply'], 'Save rebinned data, overwrite current arrays')

        wids['save_as'] = Button(panel, 'Save As New Group: ', size=(150, -1),
                                 action=self.on_saveas)
        SetTip(wids['save_as'], 'Save corrected data as new group')

        wids['save_as_name'] = wx.TextCtrl(panel, -1, self.dgroup.filename + '_rebin',
                                           size=(250, -1))

        def add_text(text, dcol=1, newrow=True):
            panel.Add(SimpleText(panel, text), dcol=dcol, newrow=newrow)

        add_text('Rebin Data for Group: ', dcol=2, newrow=False)
        panel.Add(wids['grouplist'], dcol=3)

        add_text('E0: ')
        panel.Add(wids['e0'])
        add_text(' eV', newrow=False)

        add_text('Region ')
        add_text('Start ', newrow=False)
        add_text('Stop ', newrow=False)
        add_text('Step ', newrow=False)
        add_text('Units ', newrow=False)

        add_text('Pre-Edge: ')
        panel.Add(wids['pre1'])
        panel.Add(wids['pre2'])
        panel.Add(wids['pre_step'])
        add_text(' eV', newrow=False)

        add_text('XANES: ')
        panel.Add(wids['xanes1'])
        panel.Add(wids['xanes2'])
        panel.Add(wids['xanes_step'])
        add_text(' eV', newrow=False)

        add_text('EXAFS: ')
        panel.Add(wids['exafs1'])
        panel.Add(wids['exafs2'])
        panel.Add(wids['exafs_step'])
        add_text('1/\u212B', newrow=False)

        # panel.Add(wids['apply'], dcol=2, newrow=True)
        panel.Add(wids['save_as'],  dcol=2, newrow=True)
        panel.Add(wids['save_as_name'], dcol=3)
        panel.Add(Button(panel, 'Done', size=(150, -1), action=self.onDone),
                  newrow=True)
        panel.pack()

        fit_dialog_window(self, panel)

        self.on_rebin()
        self.plot_results()

    def onDone(self, event=None):
        self.Destroy()


    def on_groupchoice(self, event=None):
        self.dgroup = self.controller.get_group(self.wids['grouplist'].GetStringSelection())
        self.wids['save_as_name'].SetValue(self.dgroup.filename + '_rebin')
        self.plot_results()

    def on_rebin(self, event=None, name=None, value=None):
        wids = self.wids
        if name == 'pre2':
            val = wids['pre2'].GetValue()
            wids['xanes1'].SetValue(val, act=False)
        elif name == 'xanes1':
            val = wids['xanes1'].GetValue()
            wids['pre2'].SetValue(val, act=False)
        elif name == 'xanes2':
            val = wids['xanes2'].GetValue()
            wids['exafs1'].SetValue(etok(val), act=False)
        elif name == 'exafs1':
            val = wids['exafs1'].GetValue()
            wids['xanes2'].SetValue(ktoe(val), act=False)

        e0 = wids['e0'].GetValue()
        args = dict(group=self.dgroup.groupname, e0=e0,
                    pre1=wids['pre1'].GetValue(),
                    pre2=wids['pre2'].GetValue(),
                    pre_step=wids['pre_step'].GetValue(),
                    exafs1=ktoe(wids['exafs1'].GetValue()),
                    exafs2=ktoe(wids['exafs2'].GetValue()),
                    exafs_kstep=wids['exafs_step'].GetValue(),
                    xanes_step=wids['xanes_step'].GetValue())

        # do rebin:
        cmd = """rebin_xafs({group}, e0={e0:f}, pre1={pre1:f}, pre2={pre2:f},
        pre_step={pre_step:f}, xanes_step={xanes_step:f}, exafs1={exafs1:f},
        exafs2={exafs2:f}, exafs_kstep={exafs_kstep:f})""".format(**args)
        self.cmd = cmd
        self.controller.larch.eval(cmd)

        if hasattr(self.dgroup, 'rebinned'):
            xnew = self.dgroup.rebinned.energy
            ynew = self.dgroup.rebinned.mu
            yerr = self.dgroup.rebinned.delta_mu
            self.data = xnew, ynew, yerr, e0
            self.plot_results()

    def on_apply(self, event=None):
        xdat, ydat, yerr, e0 = self.data
        dgroup = self.dgroup
        dgroup.energy = dgroup.xdat = xdat
        dgroup.mu     = dgroup.ydat = ydat
        dgroup.journal.add('rebin_command ', self.cmd)
        self.parent.process_normalization(dgroup)
        self.plot_results()

    def on_saveas(self, event=None):
        wids = self.wids
        fname = wids['grouplist'].GetStringSelection()
        new_fname = wids['save_as_name'].GetValue()
        ngroup = self.controller.copy_group(fname, new_filename=new_fname)
        xdat, ydat, yerr, de0 = self.data
        ngroup.energy = ngroup.xdat = xdat
        ngroup.mu     = ngroup.ydat = ydat

        ogroup = self.controller.get_group(fname)
        olddesc = ogroup.journal.get('source_desc').value

        ngroup.delta_mu = getattr(ngroup, 'yerr', 1.0)
        self.parent.process_normalization(ngroup)

        self.parent.install_group(ngroup, journal=ogroup.journal)
        ngroup.journal.add('source_desc', f"rebinned({olddesc})")
        ngroup.journal.add('rebin_command ', self.cmd)

    def on_done(self, event=None):
        self.Destroy()

    def plot_results(self):
        ppanel = self.controller.get_display(stacked=False).panel
        xnew, ynew, yerr, e0 = self.data
        dgroup = self.dgroup
        zoom_limits = get_zoomlimits(ppanel, dgroup)
        path, fname = os.path.split(dgroup.filename)

        opts = {}
        if self.controller.plot_erange is not None:
            opts['xmin'] = dgroup.e0 + self.controller.plot_erange[0]
            opts['xmax'] = dgroup.e0 + self.controller.plot_erange[1]

        ppanel.plot(xnew, ynew, zorder=20, delay_draw=True, marker='square',
                    linewidth=3, title='Enegy rebinning:\n %s' % fname,
                    label='rebinned', xlabel=plotlabels.energy,
                    ylabel=plotlabels.mu, **opts)

        xold, yold = self.dgroup.energy, self.dgroup.mu
        ppanel.oplot(xold, yold, zorder=10, delay_draw=False,
                     marker='o', markersize=4, linewidth=2.0,
                     label='original', show_legend=True, **opts)
        set_zoomlimits(ppanel, zoom_limits) or ppanel.unzoom_all()
        ppanel.canvas.draw()

    def GetResponse(self):
        raise AttributeError("use as non-modal dialog!")

class SmoothDataDialog(wx.Dialog):
    """dialog for smoothing data"""
    def __init__(self, parent, controller, **kws):

        self.parent = parent
        self.controller = controller
        self.dgroup = self.controller.get_group()
        groupnames = list(self.controller.file_groups.keys())

        self.data = [self.dgroup.energy[:], self.dgroup.mu[:]]


        wx.Dialog.__init__(self, parent, wx.ID_ANY, size=(550, 400),
                           title="Smooth mu(E) Data")
        self.SetFont(Font(FONTSIZE))
        panel = GridPanel(self, ncols=3, nrows=4, pad=4, itemstyle=LEFT)

        self.wids = wids = {}

        wids['grouplist'] = Choice(panel, choices=groupnames, size=(250, -1),
                                action=self.on_groupchoice)

        wids['grouplist'].SetStringSelection(self.dgroup.filename)
        SetTip(wids['grouplist'], 'select a new group, clear undo history')

        smooth_ops = ('None', 'Boxcar', 'Savitzky-Golay', 'Convolution')
        conv_ops  = ('Lorenztian', 'Gaussian')

        self.smooth_op = Choice(panel, choices=smooth_ops, size=(150, -1),
                                action=self.on_smooth)
        self.smooth_op.SetSelection(0)

        self.conv_op = Choice(panel, choices=conv_ops, size=(150, -1),
                                action=self.on_smooth)
        self.conv_op.SetSelection(0)

        opts  = dict(size=(50, -1), act_on_losefocus=True, odd_only=False)

        self.sigma = FloatCtrl(panel, value=1, precision=2, minval=0, **opts)
        self.par_n = FloatCtrl(panel, value=2, precision=0, minval=1, **opts)
        self.par_o = FloatCtrl(panel, value=1, precision=0, minval=1, **opts)

        for fc in (self.sigma, self.par_n, self.par_o):
            fc.SetAction(self.on_smooth)

        self.message = SimpleText(panel, label='         ', size=(200, -1))

        # wids['apply'] = Button(panel, 'Save / Overwrite', size=(150, -1),
        #                       action=self.on_apply)
        # SetTip(wids['apply'], 'Save corrected data, overwrite current arrays')

        wids['save_as'] = Button(panel, 'Save As New Group: ', size=(150, -1),
                           action=self.on_saveas)
        SetTip(wids['save_as'], 'Save corrected data as new group')

        wids['save_as_name'] = wx.TextCtrl(panel, -1, self.dgroup.filename + '_smooth',
                                           size=(250, -1))

        def add_text(text, dcol=1, newrow=True):
            panel.Add(SimpleText(panel, text), dcol=dcol, newrow=newrow)

        add_text('Smooth Data for Group: ', newrow=False)
        panel.Add(wids['grouplist'], dcol=5)

        add_text('Smoothing Method: ')
        panel.Add(self.smooth_op)
        add_text(' n= ', newrow=False)
        panel.Add(self.par_n)
        add_text(' order= ', newrow=False)
        panel.Add(self.par_o)

        add_text('Convolution Form: ')
        panel.Add(self.conv_op)
        add_text(' sigma: ', newrow=False)
        panel.Add(self.sigma)

        panel.Add((10, 10), newrow=True)
        panel.Add(self.message, dcol=5)

        # panel.Add(wids['apply'], newrow=True)

        panel.Add(wids['save_as'],  newrow=True)
        panel.Add(wids['save_as_name'], dcol=5)
        panel.Add(Button(panel, 'Done', size=(150, -1), action=self.onDone),
                  newrow=True)
        panel.pack()
        fit_dialog_window(self, panel)

        self.plot_results()

    def onDone(self, event=None):
        self.Destroy()


    def on_groupchoice(self, event=None):
        self.dgroup = self.controller.get_group(self.wids['grouplist'].GetStringSelection())
        self.wids['save_as_name'].SetValue(self.dgroup.filename + '_smooth')
        self.plot_results()

    def on_smooth(self, event=None, value=None):
        smoothop = self.smooth_op.GetStringSelection().lower()

        convop   = self.conv_op.GetStringSelection()
        self.message.SetLabel('')
        self.par_n.SetMin(1)
        self.par_n.odd_only = False
        par_n = int(self.par_n.GetValue())
        par_o = int(self.par_o.GetValue())
        sigma = self.sigma.GetValue()
        cmd = '{group:s}.mu' # No smoothing
        if smoothop.startswith('box'):
            self.par_n.Enable()
            cmd = "boxcar({group:s}.mu, {par_n:d})"
        elif smoothop.startswith('savi'):
            self.par_n.Enable()
            self.par_n.odd_only = True
            self.par_o.Enable()

            x0 = max(par_o + 1, par_n)
            if x0 % 2 == 0:
                x0 += 1
            self.par_n.SetMin(par_o + 1)
            if par_n != x0:
                self.par_n.SetValue(x0)
            self.message.SetLabel('n must odd and > order+1')

            cmd = "savitzky_golay({group:s}.mu, {par_n:d}, {par_o:d})"

        elif smoothop.startswith('conv'):
            estep = find_energy_step(self.data[0])
            cmd = "smooth({group:s}.energy, {group:s}.mu, xstep={estep:f}, sigma={sigma:f}, form='{convop:s}')"
        self.cmd = cmd.format(group=self.dgroup.groupname, convop=convop,
                              estep=estep, sigma=sigma, par_n=par_n, par_o=par_o)

        self.controller.larch.eval("_tmpy = %s" % self.cmd)
        self.data = self.dgroup.energy[:], self.controller.symtable._tmpy
        self.plot_results()

    def on_apply(self, event=None):
        xdat, ydat = self.data
        dgroup = self.dgroup
        dgroup.energy = xdat
        dgroup.mu     = ydat
        dgroup.journal.add('smooth_command', self.cmd)
        self.parent.process_normalization(dgroup)
        self.plot_results()

    def on_saveas(self, event=None):
        wids = self.wids
        fname = wids['grouplist'].GetStringSelection()
        new_fname = wids['save_as_name'].GetValue()
        ngroup = self.controller.copy_group(fname, new_filename=new_fname)

        xdat, ydat = self.data
        ngroup.energy = ngroup.xdat = xdat
        ngroup.mu     = ngroup.ydat = ydat

        ogroup = self.controller.get_group(fname)
        olddesc = ogroup.journal.get('source_desc').value

        self.parent.install_group(ngroup, journal=ogroup.journal)
        ngroup.journal.add('source_desc', f"smoothed({olddesc})")
        ngroup.journal.add('smooth_command', self.cmd)
        self.parent.process_normalization(ngroup)

    def on_done(self, event=None):
        self.Destroy()

    def plot_results(self):
        ppanel = self.controller.get_display(stacked=False).panel
        xnew, ynew = self.data
        dgroup = self.dgroup
        zoom_limits = get_zoomlimits(ppanel, dgroup)
        path, fname = os.path.split(dgroup.filename)

        opts = {}
        if self.controller.plot_erange is not None:
            opts['xmin'] = dgroup.e0 + self.controller.plot_erange[0]
            opts['xmax'] = dgroup.e0 + self.controller.plot_erange[1]

        ppanel.plot(xnew, ynew, zorder=20, delay_draw=True, marker=None,
                    linewidth=3, title='Smoothing:\n %s' % fname,
                    label='smoothed', xlabel=plotlabels.energy,
                    ylabel=plotlabels.mu, **opts)

        xold, yold = self.dgroup.energy, self.dgroup.mu
        ppanel.oplot(xold, yold, zorder=10, delay_draw=False,
                     marker='o', markersize=4, linewidth=2.0,
                     label='original', show_legend=True, **opts)
        set_zoomlimits(ppanel, zoom_limits) or ppanel.unzoom_all()
        ppanel.canvas.draw()

    def GetResponse(self):
        raise AttributeError("use as non-modal dialog!")

class DeconvolutionDialog(wx.Dialog):
    """dialog for energy deconvolution"""
    def __init__(self, parent, controller, **kws):

        self.parent = parent
        self.controller = controller
        self.dgroup = self.controller.get_group()
        groupnames = list(self.controller.file_groups.keys())

        self.data = [self.dgroup.energy[:], self.dgroup.norm[:]]


        wx.Dialog.__init__(self, parent, wx.ID_ANY, size=(550, 400),
                           title="Deconvolve mu(E) Data")
        self.SetFont(Font(FONTSIZE))
        panel = GridPanel(self, ncols=3, nrows=4, pad=4, itemstyle=LEFT)

        self.wids = wids = {}

        wids['grouplist'] = Choice(panel, choices=groupnames, size=(250, -1),
                                action=self.on_groupchoice)

        wids['grouplist'].SetStringSelection(self.dgroup.groupname)
        SetTip(wids['grouplist'], 'select a new group, clear undo history')

        deconv_ops  = ('Lorenztian', 'Gaussian')

        wids['deconv_op'] = Choice(panel, choices=deconv_ops, size=(150, -1),
                                   action=self.on_deconvolve)

        wids['esigma'] = FloatSpin(panel, value=0.5, digits=2, size=(90, -1),
                                   increment=0.1, action=self.on_deconvolve)

        #wids['apply'] = Button(panel, 'Save / Overwrite', size=(150, -1),
        #                       action=self.on_apply)
        #SetTip(wids['apply'], 'Save corrected data, overwrite current arrays')

        wids['save_as'] = Button(panel, 'Save As New Group: ', size=(150, -1),
                           action=self.on_saveas)
        SetTip(wids['save_as'], 'Save corrected data as new group')

        wids['save_as_name'] = wx.TextCtrl(panel, -1, self.dgroup.filename + '_deconv',
                                           size=(250, -1))

        def add_text(text, dcol=1, newrow=True):
            panel.Add(SimpleText(panel, text), dcol=dcol, newrow=newrow)

        add_text('Deconvolve Data for Group: ', newrow=False)
        panel.Add(wids['grouplist'], dcol=5)

        add_text('Functional Form: ')
        panel.Add(wids['deconv_op'])

        add_text(' sigma= ')
        panel.Add(wids['esigma'])
        # panel.Add(wids['apply'], newrow=True)
        panel.Add(wids['save_as'],  newrow=True)
        panel.Add(wids['save_as_name'], dcol=5)
        panel.Add(Button(panel, 'Done', size=(150, -1), action=self.onDone),
                  newrow=True)
        panel.pack()

        fit_dialog_window(self, panel)
        self.plot_results()

    def onDone(self, event=None):
        self.Destroy()

    def on_saveas(self, event=None):
        wids = self.wids
        fname = wids['grouplist'].GetStringSelection()
        new_fname = wids['save_as_name'].GetValue()
        ngroup = self.controller.copy_group(fname, new_filename=new_fname)
        xdat, ydat = self.data
        ngroup.energy = ngroup.xdat = xdat
        ngroup.mu     = ngroup.ydat = ydat

        ogroup = self.controller.get_group(fname)
        olddesc = ogroup.journal.get('source_desc').value

        self.parent.install_group(ngroup, journal=ogroup.journal)
        ngroup.journal.add('source_desc', f"deconvolved({olddesc})")
        ngroup.journal.add('deconvolve_command', self.cmd)
        self.parent.process_normalization(ngroup)


    def on_groupchoice(self, event=None):
        self.dgroup = self.controller.get_group(self.wids['grouplist'].GetStringSelection())
        self.wids['save_as_name'].SetValue(self.dgroup.filename + '_deconv')
        self.plot_results()

    def on_deconvolve(self, event=None, value=None):
        deconv_form  = self.wids['deconv_op'].GetStringSelection()

        esigma = self.wids['esigma'].GetValue()

        dopts = [self.dgroup.groupname,
                 "form='%s'" % (deconv_form),
                 "esigma=%.4f" % (esigma)]
        self.cmd = "xas_deconvolve(%s)" % (', '.join(dopts))
        self.controller.larch.eval(self.cmd)

        self.data = self.dgroup.energy[:], self.dgroup.deconv[:]
        self.plot_results()

    def on_apply(self, event=None):
        xdat, ydat = self.data
        dgroup = self.dgroup
        dgroup.energy = xdat
        dgroup.mu     = ydat
        dgroup.journal.add('deconvolve_command ', self.cmd)
        self.parent.process_normalization(dgroup)
        self.plot_results()

    def plot_results(self):
        ppanel = self.controller.get_display(stacked=False).panel
        xnew, ynew = self.data
        dgroup = self.dgroup
        zoom_limits = get_zoomlimits(ppanel, dgroup)
        path, fname = os.path.split(dgroup.filename)

        opts = {}
        if self.controller.plot_erange is not None:
            opts['xmin'] = dgroup.e0 + self.controller.plot_erange[0]
            opts['xmax'] = dgroup.e0 + self.controller.plot_erange[1]

        ppanel.plot(xnew, ynew, zorder=20, delay_draw=True, marker=None,
                    linewidth=3, title='Deconvolving:\n %s' % fname,
                    label='deconvolved', xlabel=plotlabels.energy,
                    ylabel=plotlabels.mu, **opts)

        xold, yold = self.dgroup.energy, self.dgroup.norm
        ppanel.oplot(xold, yold, zorder=10, delay_draw=False,
                     marker='o', markersize=4, linewidth=2.0,
                     label='original', show_legend=True, **opts)
        set_zoomlimits(ppanel, zoom_limits) or ppanel.unzoom_all()
        ppanel.canvas.draw()

    def GetResponse(self):
        raise AttributeError("use as non-modal dialog!")

class DeglitchDialog(wx.Dialog):
    """dialog for deglitching or removing unsightly data points"""
    def __init__(self, parent, controller, **kws):
        self.parent = parent
        self.controller = controller
        self.wids = {}
        self.dgroup = self.controller.get_group()
        groupnames = list(self.controller.file_groups.keys())

        self.reset_data_history()
        xdat, ydat = self.data

        xrange = (max(xdat) - min(xdat))
        xmax = int(max(xdat) + xrange/5.0)
        xmin = int(min(xdat) - xrange/5.0)

        lastx, lasty = self.controller.get_cursor()
        if lastx is None:
            lastx = max(xdat)

        wx.Dialog.__init__(self, parent, wx.ID_ANY, size=(550, 400),
                           title="Select Points to Remove")
        self.SetFont(Font(FONTSIZE))
        panel = GridPanel(self, ncols=3, nrows=4, pad=4, itemstyle=LEFT)
        wids = self.wids

        wids['grouplist'] = Choice(panel, choices=groupnames, size=(250, -1),
                                action=self.on_groupchoice)

        wids['grouplist'].SetStringSelection(self.dgroup.filename)
        SetTip(wids['grouplist'], 'select a new group, clear undo history')

        br_xlast = Button(panel, 'Remove point', size=(125, -1),
                          action=partial(self.on_remove, opt='x'))

        br_range = Button(panel, 'Remove range', size=(125, -1),
                          action=partial(self.on_remove, opt='range'))

        undo = Button(panel, 'Undo remove', size=(125, -1),
                      action=self.on_undo)
        #wids['apply'] = Button(panel, 'Save / Overwrite', size=(150, -1),
        #                       action=self.on_apply)
        #SetTip(wids['apply'], '''Save deglitched, overwrite current arrays,
#clear undo history''')

        wids['save_as'] = Button(panel, 'Save As New Group: ', size=(150, -1),
                                 action=self.on_saveas)
        SetTip(wids['save_as'], 'Save deglitched data as new group')

        wids['save_as_name'] = wx.TextCtrl(panel, -1, self.dgroup.filename + '_clean',
                                           size=(250, -1))

        self.history_message = SimpleText(panel, '')

        opts  = dict(size=(125, -1), digits=2, increment=0.1, action=None)
        for wname in ('xlast', 'range1', 'range2'):
            if wname == 'range2': lastx += 1
            pin_callback = partial(self.on_pinvalue, opt=wname)
            fspin, pinbtn = add_floatspin(wname, lastx, panel,
                                          with_pin=True, xasmain=self.parent,
                                          callback=pin_callback, **opts)
            wids[wname] = fspin
            wids[wname+'_pin'] = pinbtn

        self.choice_range = Choice(panel, choices=('above', 'below', 'between'),
                                    size=(90, -1), action=self.on_rangechoice)

        self.choice_range.SetStringSelection('above')
        wids['range2'].Disable()

        wids['plotopts'] = Choice(panel, choices=list(DEGLITCH_PLOTS.keys()),
                                  size=(175, -1),
                                  action=self.on_plotchoice)

        wids['plotopts'].SetStringSelection(NORM_MU)

        def add_text(text, dcol=1, newrow=True):
            panel.Add(SimpleText(panel, text), dcol=dcol, newrow=newrow)

        add_text('Deglitch Data for Group: ', dcol=2, newrow=False)
        panel.Add(wids['grouplist'], dcol=5)

        add_text('Single Energy : ', dcol=2)
        panel.Add(wids['xlast'])
        panel.Add(wids['xlast_pin'])
        panel.Add(br_xlast)

        add_text('Plot Data as:  ', dcol=2)
        panel.Add(wids['plotopts'], dcol=5)

        add_text('Energy Range : ')
        panel.Add(self.choice_range)
        panel.Add(wids['range1'])
        panel.Add(wids['range1_pin'])
        panel.Add(br_range)

        panel.Add((10, 10), dcol=2, newrow=True)
        panel.Add(wids['range2'])
        panel.Add(wids['range2_pin'])

        # panel.Add(wids['apply'], dcol=2, newrow=True)

        panel.Add(wids['save_as'], dcol=2, newrow=True)
        panel.Add(wids['save_as_name'], dcol=4)
        panel.Add(Button(panel, 'Done', size=(150, -1), action=self.onDone),
                  dcol=2, newrow=True)
        panel.Add(self.history_message, dcol=2)
        panel.Add(undo)

        panel.pack()

        fit_dialog_window(self, panel)
        self.plot_results(use_zoom=False)

    def onDone(self, event=None):
        self.Destroy()

    def reset_data_history(self):
        plottype = 'norm'
        if 'plotopts' in self.wids:
            plotstr = self.wids['plotopts'].GetStringSelection()
            plottype = DEGLITCH_PLOTS[plotstr]
        self.data = self.get_xydata(datatype=plottype)
        self.xmasks = [np.ones(len(self.data[0]), dtype=bool)]

    def get_xydata(self, datatype='mu'):
        if hasattr(self.dgroup, 'energy'):
            xdat = self.dgroup.energy[:]
        else:
            xdat = self.dgroup.xdat[:]
        ydat = self.dgroup.ydat[:]
        if datatype == 'mu' and hasattr(self.dgroup, 'mu'):
            ydat = self.dgroup.mu[:]
        elif datatype == 'norm':
            if not hasattr(self.dgroup, 'norm'):
                self.parent.process_normalization(dgroup)
            ydat = self.dgroup.norm[:]
        elif datatype in ('chie', 'chiew'):
            if not hasattr(self.dgroup, 'chie'):
                self.parent.process_exafs(self.dgroup)
            ydat = self.dgroup.chie[:]
            if datatype == 'chiew':
                ydat = self.dgroup.chie[:] * (xdat-self.dgroup.e0)
        return (xdat, ydat)

    def on_groupchoice(self, event=None):
        self.dgroup = self.controller.get_group(self.wids['grouplist'].GetStringSelection())
        self.wids['save_as_name'].SetValue(self.dgroup.filename + '_clean')
        self.reset_data_history()
        self.plot_results(use_zoom=True)

    def on_rangechoice(self, event=None):
        sel = self.choice_range.GetStringSelection()
        self.wids['range2'].Enable(sel == 'between')


    def on_plotchoice(self, event=None):
        plotstr = self.wids['plotopts'].GetStringSelection()
        plottype = DEGLITCH_PLOTS[plotstr]
        self.data = self.get_xydata(datatype=plottype)
        self.plot_results()

    def on_pinvalue(self, opt='__', xsel=None, **kws):
        if xsel is not None and opt in self.wids:
            self.wids[opt].SetValue(xsel)

    def on_remove(self, event=None, opt=None):
        xwork, ywork = self.data
        mask = copy.deepcopy(self.xmasks[-1])
        if opt == 'x':
            bad = index_nearest(xwork, self.wids['xlast'].GetValue())
            mask[bad] = False
        elif opt == 'range':
            rchoice = self.choice_range.GetStringSelection().lower()
            x1 = index_nearest(xwork, self.wids['range1'].GetValue())
            x2 = None
            if rchoice == 'below':
                x2, x1 = x1, x2
            elif rchoice == 'between':
                x2 = index_nearest(xwork, self.wids['range2'].GetValue())
                if x1 > x2:
                    x1, x2 = x2, x1
            mask[x1:x2] = False
        self.xmasks.append(mask)
        self.plot_results()

    def on_undo(self, event=None):
        if len(self.xmasks) == 1:
            self.xmasks = [np.ones(len(self.data[0]), dtype=bool)]
        else:
            self.xmasks.pop()
        self.plot_results()

    def on_apply(self, event=None):
        xdat, ydat = self.get_xydata(datatype='raw')
        mask = self.xmasks[-1]
        dgroup = self.dgroup
        energies_removed  = xdat[np.where(~mask)].tolist()
        dgroup.energy = dgroup.xdat = xdat[mask]
        dgroup.mu     = dgroup.ydat = ydat[mask]
        self.reset_data_history()
        dgroup.journal.add('deglitch_removed_energies', energies_removed)
        self.parent.process_normalization(dgroup)
        self.plot_results()

    def on_saveas(self, event=None):
        fname = self.wids['grouplist'].GetStringSelection()
        new_fname = self.wids['save_as_name'].GetValue()
        ngroup = self.controller.copy_group(fname, new_filename=new_fname)
        xdat, ydat = self.get_xydata(datatype='mu')
        mask = self.xmasks[-1]
        energies_removed  = xdat[np.where(~mask)].tolist()

        ngroup.energy = ngroup.xdat = xdat[mask]
        ngroup.mu     = ngroup.ydat = ydat[mask]
        ngroup.energy_orig = 1.0*ngroup.energy

        ogroup = self.controller.get_group(fname)
        olddesc = ogroup.journal.get('source_desc').value

        self.parent.install_group(ngroup, journal=ogroup.journal)
        ngroup.journal.add('source_desc', f"deglitched({olddesc})")
        ngroup.journal.add('deglitch_removed_energies', energies_removed)

        self.parent.process_normalization(ngroup)

    def plot_results(self, use_zoom=True):
        ppanel = self.controller.get_display(stacked=False).panel

        xdat, ydat = self.data

        xmin = min(xdat) - 0.025*(max(xdat) - min(xdat))
        xmax = max(xdat) + 0.025*(max(xdat) - min(xdat))
        ymin = min(ydat) - 0.025*(max(ydat) - min(ydat))
        ymax = max(ydat) + 0.025*(max(ydat) - min(ydat))

        dgroup = self.dgroup

        path, fname = os.path.split(dgroup.filename)

        plotstr = self.wids['plotopts'].GetStringSelection()
        plottype = DEGLITCH_PLOTS[plotstr]

        xlabel=plotlabels.energy
        if plottype in ('chie', 'chiew'):
            xmin = self.dgroup.e0
            xlabel = xlabel=plotlabels.ewithk

        opts = dict(xlabel=xlabel, title='De-glitching:\n %s' % fname)

        ylabel =  {'mu': plotlabels.mu,
                   'norm': plotlabels.norm,
                   'chie':  plotlabels.chie,
                   'chiew': plotlabels.chiew.format(1),
                   }.get(plottype, plotlabels.norm)

        dgroup.plot_xlabel = xlabel
        dgroup.plot_ylabel = ylabel

        zoom_limits = None
        if use_zoom:
            zoom_limits = get_zoomlimits(ppanel, dgroup)

        ppanel.plot(xdat, ydat, zorder=10, marker=None, linewidth=3,
                    label='original', ylabel=ylabel, **opts)

        if len(self.xmasks) > 1:
            mask = self.xmasks[-1]
            ppanel.oplot(xdat[mask], ydat[mask], zorder=15,
                         marker='o', markersize=3, linewidth=2.0,
                         label='current', show_legend=True, **opts)

        def ek_formatter(x, pos):
            ex = float(x) - self.dgroup.e0
            s = '' if ex < 0 else '\n[%.1f]' % (etok(ex))
            return r"%1.4g%s" % (x, s)

        set_zoomlimits(ppanel, zoom_limits) or ppanel.unzoom_all()
        if plottype in ('chie', 'chiew'):
            ppanel.axes.xaxis.set_major_formatter(FuncFormatter(ek_formatter))
        ppanel.canvas.draw()

        self.history_message.SetLabel('%i items in history' % (len(self.xmasks)-1))

    def GetResponse(self):
        raise AttributeError("use as non-modal dialog!")


SPECCALC_SETUP = """#From SpectraCalc dialog:
_x = {group:s}.{xname:s}
a = {group:s}.{yname:s}
b = c = d = e = f = g = None
"""

SPECCALC_INTERP = "{key:s} = interp({group:s}.{xname:s}, {group:s}.{yname:s}, _x)"
SPECCALC_PLOT = """plot(_x, ({expr:s}), label='{expr:s}', new=True,
   show_legend=True, xlabel='{xname:s}', title='Spectral Calculation')"""

SPECCALC_SAVE = """{new:s} = copy_xafs_group({group:s})
{new:s}.groupname = '{new:s}'
{new:s}.mu = ({expr:s})
{new:s}.filename = '{fname:s}'
{new:s}.journal.add('calc_groups', {group_map:s})
{new:s}.journal.add('calc_arrayname', '{yname:s}')
{new:s}.journal.add('calc_expression', '{expr:s}')
del _x, a, b, c, d, e, f, g"""


class SpectraCalcDialog(wx.Dialog):
    """dialog for adding and subtracting spectra"""
    def __init__(self, parent, controller, **kws):

        self.parent = parent
        self.controller = controller
        self.dgroup = self.controller.get_group()
        self.group_a = None
        groupnames = list(self.controller.file_groups.keys())

        self.data = [self.dgroup.energy[:], self.dgroup.norm[:]]
        xmin = min(self.dgroup.energy)
        xmax = max(self.dgroup.energy)
        e0val = getattr(self.dgroup, 'e0', xmin)

        wx.Dialog.__init__(self, parent, wx.ID_ANY, size=(475, 525),
                           title="Spectra Calculations: Add, Subtract Spectra")
        self.SetFont(Font(FONTSIZE))
        panel = GridPanel(self, ncols=3, nrows=4, pad=4, itemstyle=LEFT)

        def add_text(text, dcol=1, newrow=True):
            panel.Add(SimpleText(panel, text), dcol=dcol, newrow=newrow)

        self.wids = wids = {}
        array_choices = ('Normalized \u03BC(E)', 'Raw \u03BC(E)')

        wids['array'] = Choice(panel, choices=array_choices, size=(250, -1))

        add_text('Array to use: ',  newrow=True)
        panel.Add(wids['array'], dcol=2)

        # group 'a' cannot be none, and defaults to current group
        gname = 'a'
        wname = 'group_%s' % gname
        wids[wname] = Choice(panel, choices=groupnames, size=(250, -1))
        wids[wname].SetStringSelection(self.dgroup.filename)
        add_text('   %s = ' % gname,  newrow=True)
        panel.Add(wids[wname], dcol=2)

        groupnames.insert(0, 'None')
        for gname in ('b', 'c', 'd', 'e', 'f', 'g'):
            wname = 'group_%s' % gname
            wids[wname] = Choice(panel, choices=groupnames, size=(250, -1))
            wids[wname].SetSelection(0)
            add_text('   %s = ' % gname,  newrow=True)
            panel.Add(wids[wname], dcol=2)

        wids['formula'] = wx.TextCtrl(panel, -1, 'a-b', size=(250, -1))
        add_text('Expression = ',  newrow=True)
        panel.Add(wids['formula'], dcol=2)

        wids['docalc'] = Button(panel, 'Calculate',
                                size=(150, -1), action=self.on_docalc)

        panel.Add(wids['docalc'], dcol=2, newrow=True)

        wids['save_as'] = Button(panel, 'Save As New Group: ', size=(150, -1),
                           action=self.on_saveas)
        SetTip(wids['save_as'], 'Save as new group')

        wids['save_as_name'] = wx.TextCtrl(panel, -1,
                                           self.dgroup.filename + '_calc',
                                           size=(250, -1))
        panel.Add(wids['save_as'], newrow=True)
        panel.Add(wids['save_as_name'], dcol=2)
        wids['save_as'].Disable()
        panel.Add(Button(panel, 'Done', size=(150, -1), action=self.onDone),
                  newrow=True)
        panel.pack()
        fit_dialog_window(self, panel)

    def onDone(self, event=None):
        self.Destroy()

    def on_docalc(self, event=None):
        self.expr = self.wids['formula'].GetValue()

        self.yname = 'mu'
        if self.wids['array'].GetStringSelection().lower().startswith('norm'):
            self.yname = 'norm'

        groups = {}
        for aname in ('a', 'b', 'c', 'd', 'e', 'f', 'g'):
            fname = self.wids['group_%s' % aname].GetStringSelection()
            if fname not in (None, 'None'):
                grp = self.controller.get_group(fname)
                groups[aname] = grp

        self.group_map = {key: group.groupname for key, group in groups.items()}
        # note: 'a' cannot be None, all others can be None
        group_a = self.group_a = groups.pop('a')
        xname = 'energy'
        if not hasattr(group_a, xname):
            xname = 'xdat'

        cmds = [SPECCALC_SETUP.format(group=group_a.groupname,
                                      xname=xname, yname=self.yname)]

        for key, group in groups.items():
            cmds.append(SPECCALC_INTERP.format(key=key, group=group.groupname,
                                               xname=xname, yname=self.yname))

        cmds.append(SPECCALC_PLOT.format(expr=self.expr, xname=xname))
        self.controller.larch.eval('\n'.join(cmds))
        self.wids['save_as'].Enable()

    def on_saveas(self, event=None):
        wids = self.wids
        _larch = self.controller.larch
        fname = wids['group_a'].GetStringSelection()
        new_fname =self.wids['save_as_name'].GetValue()
        new_gname = file2groupname(new_fname, slen=5, symtable=_larch.symtable)

        gmap = []
        for k, v in self.group_map.items():
            gmap.append(f'"{k}": "{v}"')
        gmap = '{%s}' % (', '.join(gmap))

        _larch.eval(SPECCALC_SAVE.format(new=new_gname, fname=new_fname,
                                         group=self.group_a.groupname,
                                         group_map=gmap,
                                         yname=self.yname, expr=self.expr))


        journal={'source_desc': f"{new_fname}: calc({self.expr})",
                 'calc_groups': gmap,  'calc_expression': self.expr}

        ngroup = getattr(_larch.symtable, new_gname, None)
        if ngroup is not None:
            self.parent.install_group(ngroup, source=journal['source_desc'],
                                      journal=journal)

    def GetResponse(self):
        raise AttributeError("use as non-modal dialog!")

class EnergyUnitsDialog(wx.Dialog):
    """dialog for selecting, changing energy units, forcing data to eV"""
    unit_choices = ['eV', 'keV', 'deg', 'steps']

    def __init__(self, parent, energy_array, unitname='eV',dspace=1, **kws):

        self.parent = parent
        self.energy = 1.0*energy_array

        title = "Select Energy Units to convert to 'eV'"
        wx.Dialog.__init__(self, parent, wx.ID_ANY, title=title)
        self.SetFont(Font(FONTSIZE))
        panel = GridPanel(self, ncols=3, nrows=4, pad=2, itemstyle=LEFT)

        self.en_units = Choice(panel, choices=self.unit_choices, size=(125, -1),
                               action=self.onUnits)
        self.en_units.SetStringSelection(unitname)
        self.mono_dspace = FloatCtrl(panel, value=dspace, minval=0, maxval=100.0,
                                     precision=6, size=(125, -1))
        self.steps2deg  = FloatCtrl(panel, value=1.0, minval=0,
                                     precision=1, size=(125, -1))

        self.mono_dspace.Disable()
        self.steps2deg.Disable()

        panel.Add(SimpleText(panel, 'Energy Units : '), newrow=True)
        panel.Add(self.en_units)

        panel.Add(SimpleText(panel, 'Mono D spacing : '), newrow=True)
        panel.Add(self.mono_dspace)

        panel.Add(SimpleText(panel, 'Mono Steps per Degree : '), newrow=True)
        panel.Add(self.steps2deg)
        panel.Add((5, 5))

        panel.Add(OkCancel(panel), dcol=2, newrow=True)
        panel.pack()

        fit_dialog_window(self, panel)


    def onUnits(self, event=None):
        units = self.en_units.GetStringSelection()
        self.steps2deg.Enable(units == 'steps')
        self.mono_dspace.Enable(units in ('steps', 'deg'))

    def GetResponse(self, master=None, gname=None, ynorm=True):
        self.Raise()
        response = namedtuple('EnergyUnitsResponse',
                              ('ok', 'units', 'energy', 'dspace'))
        ok, units, en, dspace = False, 'eV', None, -1

        if self.ShowModal() == wx.ID_OK:
            units = self.en_units.GetStringSelection()
            if units == 'eV':
                en = self.energy
            elif units == 'keV':
                en = self.energy * 1000.0
            elif units in ('steps', 'deg'):
                dspace = float(self.mono_dspace.GetValue())
                if units == 'steps':
                    self.energy /= self.steps2deg.GetValue()
                en = PLANCK_HC/(2*dspace*np.sin(self.energy * DEG2RAD))
            ok = True
        return response(ok, units, en, dspace)

class MergeDialog(wx.Dialog):
    """dialog for merging groups"""
    ychoices = ['raw mu(E)', 'normalized mu(E)']

    def __init__(self, parent, groupnames, outgroup='merge', **kws):
        title = "Merge %i Selected Groups" % (len(groupnames))
        wx.Dialog.__init__(self, parent, wx.ID_ANY, title=title)

        panel = GridPanel(self, ncols=3, nrows=4, pad=2, itemstyle=LEFT)

        self.master_group = Choice(panel, choices=groupnames, size=(250, -1))
        self.yarray_name  = Choice(panel, choices=self.ychoices, size=(250, -1))
        self.group_name   = wx.TextCtrl(panel, -1, outgroup,  size=(250, -1))

        panel.Add(SimpleText(panel, 'Match Energy to : '), newrow=True)
        panel.Add(self.master_group)

        panel.Add(SimpleText(panel, 'Array to merge  : '), newrow=True)
        panel.Add(self.yarray_name)

        panel.Add(SimpleText(panel, 'New group name  : '), newrow=True)
        panel.Add(self.group_name)

        panel.Add(OkCancel(panel), dcol=2, newrow=True)

        panel.pack()
        fit_dialog_window(self, panel)


    def GetResponse(self, master=None, gname=None, ynorm=True):
        self.Raise()
        response = namedtuple('MergeResponse', ('ok', 'master', 'ynorm', 'group'))
        ok = False
        if self.ShowModal() == wx.ID_OK:
            master= self.master_group.GetStringSelection()
            ynorm = 'norm' in self.yarray_name.GetStringSelection().lower()
            gname = self.group_name.GetValue()
            ok = True
        return response(ok, master, ynorm, gname)


class ExportCSVDialog(wx.Dialog):
    """dialog for exporting groups to CSV file"""

    def __init__(self, parent, groupnames, **kws):
        title = "Export Selected Groups"
        wx.Dialog.__init__(self, parent, wx.ID_ANY, title=title)
        self.SetFont(Font(FONTSIZE))
        self.xchoices = {'Energy': 'energy',
                         'k': 'k',
                         'R': 'r',
                         'q': 'q'}

        self.ychoices = {'normalized mu(E)': 'norm',
                         'raw mu(E)': 'mu',
                         'flattened mu(E)': 'flat',
                         'd mu(E) / dE': 'dmude',
                         'chi(k)': 'chi',
                         'chi(E)': 'chie',
                         'chi(q)': 'chiq',
                         '|chi(R)|': 'chir_mag',
                         'Re[chi(R)]': 'chir_re'}

        self.delchoices = {'comma': ',',
                           'space': ' ',
                           'tab': '\t'}


        panel = GridPanel(self, ncols=3, nrows=4, pad=2, itemstyle=LEFT)

        self.save_individual_files = Check(panel, default=False, label='Save individual files', action=self.onSaveIndividualFiles)
        self.master_group = Choice(panel, choices=groupnames, size=(200, -1))
        self.xarray_name  = Choice(panel, choices=list(self.xchoices.keys()),size=(200, -1))
        self.yarray_name  = Choice(panel, choices=list(self.ychoices.keys()), size=(200, -1))
        self.del_name     = Choice(panel, choices=list(self.delchoices.keys()), size=(200, -1))


        panel.Add(self.save_individual_files, newrow=True)

        panel.Add(SimpleText(panel, 'Group for Energy Array: '), newrow=True)
        panel.Add(self.master_group)

        panel.Add(SimpleText(panel, 'X Array to Export: '), newrow=True)
        panel.Add(self.xarray_name)

        panel.Add(SimpleText(panel, 'Y Array to Export: '), newrow=True)
        panel.Add(self.yarray_name)

        panel.Add(SimpleText(panel, 'Delimeter for File: '), newrow=True)
        panel.Add(self.del_name)
        panel.Add(OkCancel(panel), dcol=2, newrow=True)
        panel.pack()
        fit_dialog_window(self, panel)

    def onSaveIndividualFiles(self, event=None):
        save_individual = self.save_individual_files.IsChecked()
        self.master_group.Enable(not save_individual)

    def GetResponse(self, master=None, gname=None, ynorm=True):
        self.Raise()
        response = namedtuple('ExportCSVResponse',
                              ('ok', 'individual', 'master', 'xarray', 'yarray', 'delim'))
        ok = False
        if self.ShowModal() == wx.ID_OK:
            individual = self.save_individual_files.IsChecked()
            master = self.master_group.GetStringSelection()
            xarray = self.xchoices[self.xarray_name.GetStringSelection()]
            yarray = self.ychoices[self.yarray_name.GetStringSelection()]
            delim  = self.delchoices[self.del_name.GetStringSelection()]
            ok = True
        return response(ok, individual, master, xarray, yarray, delim)

class QuitDialog(wx.Dialog):
    """dialog for quitting, prompting to save project"""

    def __init__(self, parent, message, **kws):
        title = "Quit Larch XAS Viewer?"
        wx.Dialog.__init__(self, parent, wx.ID_ANY, title=title, size=(500, 150))
        self.SetFont(Font(FONTSIZE))
        self.needs_save = True
        panel = GridPanel(self, ncols=3, nrows=4, pad=2, itemstyle=LEFT)

        status, filename, stime = message
        warn_msg = 'All work in this session will be lost!'

        panel.Add((5, 5))
        if len(stime) > 2:
            status = f"{status} at {stime} to file"
            warn_msg = 'Changes made after that will be lost!'

        panel.Add(wx.StaticText(panel, label=status), dcol=2)

        if len(filename) > 0:
            if filename.startswith("'") and filename.endswith("'"):
                filename = filename[1:-1]
            panel.Add((15, 5), newrow=True)
            panel.Add(wx.StaticText(panel, label=filename), dcol=2)

        panel.Add((5, 5), newrow=True)
        panel.Add(wx.StaticText(panel, label=warn_msg), dcol=2)
        panel.Add(HLine(panel, size=(500, 3)), dcol=3, newrow=True)
        panel.Add((5, 5), newrow=True)
        panel.Add(OkCancel(panel), dcol=2, newrow=True)
        panel.pack()

        fit_dialog_window(self, panel)

    def GetResponse(self):
        self.Raise()
        response = namedtuple('QuitResponse', ('ok',))
        ok = (self.ShowModal() == wx.ID_OK)
        return response(ok,)

class RenameDialog(wx.Dialog):
    """dialog for renaming group"""
    def __init__(self, parent, oldname,  **kws):
        title = "Rename Group %s" % (oldname)
        wx.Dialog.__init__(self, parent, wx.ID_ANY, title=title)
        self.SetFont(Font(FONTSIZE))
        panel = GridPanel(self, ncols=3, nrows=4, pad=2, itemstyle=LEFT)

        self.newname   = wx.TextCtrl(panel, -1, oldname,  size=(250, -1))

        panel.Add(SimpleText(panel, 'Old Name : '), newrow=True)
        panel.Add(SimpleText(panel, oldname))
        panel.Add(SimpleText(panel, 'New Name : '), newrow=True)
        panel.Add(self.newname)
        panel.Add(OkCancel(panel), dcol=2, newrow=True)

        panel.pack()
        fit_dialog_window(self, panel)


    def GetResponse(self, newname=None):
        self.Raise()
        response = namedtuple('RenameResponse', ('ok', 'newname'))
        ok = False
        if self.ShowModal() == wx.ID_OK:
            newname = self.newname.GetValue()
            ok = True
        return response(ok, newname)

class RemoveDialog(wx.Dialog):
    """dialog for removing groups"""
    def __init__(self, parent, grouplist,  **kws):
        title = "Remove %i Selected Group" % len(grouplist)
        self.grouplist = grouplist
        wx.Dialog.__init__(self, parent, wx.ID_ANY, title=title)
        self.SetFont(Font(FONTSIZE))
        panel = GridPanel(self, ncols=3, nrows=4, pad=2, itemstyle=LEFT)

        panel.Add(SimpleText(panel, 'Remove %i Selected Groups?' % (len(grouplist))),
                  newrow=True, dcol=2)

        panel.Add(OkCancel(panel), dcol=2, newrow=True)
        panel.pack()
        fit_dialog_window(self, panel)

    def GetResponse(self, ngroups=None):
        self.Raise()
        response = namedtuple('RemoveResponse', ('ok','ngroups'))
        ok = False
        if self.ShowModal() == wx.ID_OK:
            ngroups = len(self.grouplist)
            ok = True
        return response(ok, ngroups)


class LoadSessionDialog(wx.Frame):
    """Read, show data from saved larch session"""

    xasgroups_name = '_xasgroups'
    feffgroups_name = ['_feffpaths', '_feffcache']

    def __init__(self, parent, session, filename, controller, **kws):
        self.parent = parent
        self.session = session
        self.filename = filename
        self.controller = controller
        title = f"Read Larch Session from '{filename}'"
        wx.Frame.__init__(self, parent, wx.ID_ANY, title=title)

        x0, y0 = parent.GetPosition()
        self.SetPosition((x0+450, y0+75))

        splitter  = wx.SplitterWindow(self, style=wx.SP_LIVE_UPDATE)
        splitter.SetMinimumPaneSize(250)

        leftpanel = wx.Panel(splitter)
        rightpanel = wx.Panel(splitter)

        ltop = wx.Panel(leftpanel)

        sel_none = Button(ltop, 'Select None', size=(100, 30), action=self.onSelNone)
        sel_all  = Button(ltop, 'Select All', size=(100, 30), action=self.onSelAll)
        sel_imp  = Button(ltop, 'Import Selected Data', size=(200, 30),
                          action=self.onImport)

        self.select_imported = sel_imp
        self.grouplist = FileCheckList(leftpanel, select_action=self.onShowGroup)
        set_color(self.grouplist, 'list_fg', bg='list_bg')

        tsizer = wx.GridBagSizer(2, 2)
        tsizer.Add(sel_all, (0, 0), (1, 1), LEFT, 0)
        tsizer.Add(sel_none,  (0, 1), (1, 1), LEFT, 0)
        tsizer.Add(sel_imp,  (1, 0), (1, 2), LEFT, 0)

        pack(ltop, tsizer)

        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(ltop, 0, LEFT|wx.GROW, 1)
        sizer.Add(self.grouplist, 1, LEFT|wx.GROW|wx.ALL, 1)
        pack(leftpanel, sizer)


        panel = GridPanel(rightpanel, ncols=3, nrows=4, pad=2, itemstyle=LEFT)
        self.wids = wids = {}

        top_message = 'Larch Session File: No XAFS Groups'
        symtable = controller.symtable

        self.allgroups = session.symbols.get(self.xasgroups_name, {})
        self.extra_groups = []
        for key, val in session.symbols.items():
            if key == self.xasgroups_name or key in self.feffgroups_name:
                continue
            if key in self.allgroups:
                continue
            if hasattr(val, 'energy') and hasattr(val, 'mu'):
                if key in self.allgroups.keys() or key in self.allgroups.values():
                    continue
                self.allgroups[key] = key
                self.extra_groups.append(key)


        checked = []
        for fname, gname in self.allgroups.items():
            self.grouplist.Append(fname)
            checked.append(fname)

        self.grouplist.SetCheckedStrings(checked)

        group_names = list(self.allgroups.values())
        group_names.append(self.xasgroups_name)
        group_names.extend(self.feffgroups_name)

        wids['view_conf'] = Button(panel, 'Show Session Configuration',
                                     size=(200, 30), action=self.onShowConfig)
        wids['view_cmds'] = Button(panel, 'Show Session Commands',
                                     size=(200, 30), action=self.onShowCommands)

        wids['plotopt'] = Choice(panel, choices=list(SESSION_PLOTS.keys()),
                                 action=self.onPlotChoice, size=(175, -1))

        panel.Add(wids['view_conf'], dcol=1)
        panel.Add(wids['view_cmds'], dcol=1, newrow=False)
        panel.Add(HLine(panel, size=(450, 2)), dcol=3, newrow=True)

        over_msg = 'Importing these Groups/Data will overwrite values in the current session:'
        panel.Add(SimpleText(panel, over_msg), dcol=2, newrow=True)
        panel.Add(SimpleText(panel, "Symbol Name"), dcol=1, newrow=True)
        panel.Add(SimpleText(panel, "Import/Overwrite?"), dcol=1)
        i = 0
        self.overwrite_checkboxes = {}
        for g in self.session.symbols:
            if g not in group_names and hasattr(symtable, g):
                chbox = Check(panel, default=True)
                panel.Add(SimpleText(panel, g),  dcol=1, newrow=True)
                panel.Add(chbox,  dcol=1)
                self.overwrite_checkboxes[g] = chbox

                i += 1

        panel.Add((5, 5), newrow=True)
        panel.Add(HLine(panel, size=(450, 2)), dcol=3, newrow=True)
        panel.Add(SimpleText(panel, 'Plot Type:'), newrow=True)
        panel.Add(wids['plotopt'], dcol=2, newrow=False)
        panel.pack()

        self.plotpanel = PlotPanel(rightpanel, messenger=self.plot_messages)
        self.plotpanel.SetSize((475, 450))
        plotconf = self.controller.get_config('plot')
        self.plotpanel.conf.set_theme(plotconf['theme'])
        self.plotpanel.conf.enable_grid(plotconf['show_grid'])

        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(panel, 0, LEFT, 2)
        sizer.Add(self.plotpanel, 1, LEFT, 2)

        pack(rightpanel, sizer)

        splitter.SplitVertically(leftpanel, rightpanel, 1)
        self.SetSize((750, 725))

        self.Show()
        self.Raise()

    def plot_messages(self, msg, panel=1):
        pass

    def onSelAll(self, event=None):
        self.grouplist.SetCheckedStrings(list(self.allgroups.keys()))

    def onSelNone(self, event=None):
        self.grouplist.SetCheckedStrings([])

    def onShowGroup(self, event=None):
        """column selections changed calc xdat and ydat"""
        fname = event.GetString()
        gname = self.allgroups.get(fname, None)
        if gname in self.session.symbols:
            self.plot_group(gname, fname)

    def onPlotChoice(self, event=None):
        fname = self.grouplist.GetStringSelection()
        gname = self.allgroups.get(fname, None)
        self.plot_group(gname, fname)

    def plot_group(self, gname, fname):
        grp = self.session.symbols[gname]
        plottype = SESSION_PLOTS.get(self.wids['plotopt'].GetStringSelection(), 'norm')
        xdef = np.zeros(1)
        xdat = getattr(grp, 'energy', xdef)
        ydat = getattr(grp, 'mu', xdef)
        xlabel = plotlabels.energy
        ylabel = plotlabels.mu
        if plottype == 'norm' and hasattr(grp, 'norm'):
            ydat = getattr(grp, 'norm', xdef)
            ylabel = plotlabels.norm
        elif plottype == 'chikw' and hasattr(grp, 'chi'):
            xdat = getattr(grp, 'k', xdef)
            ydat = getattr(grp, 'chi', xdef)
            ydat = ydat*xdat*xdat
            xlabel = plotlabels.chikw.format(2)

        if len(ydat) > 1:
            self.plotpanel.plot(xdat, ydat, xlabel=xlabel,
                                ylabel=ylabel, title=fname)


    def onShowConfig(self, event=None):
        DictFrame(parent=self.parent,
                  data=self.session.config,
                  title=f"Session Configuration for '{self.filename}'")

    def onShowCommands(self, event=None):
        oname = self.filename.replace('.larix', '.lar')
        wildcard='Larch Command Files (*.lar)|*.lar'
        text = '\n'.join(self.session.command_history)
        ReportFrame(parent=self.parent,
                    text=text,
                    title=f"Session Commands from '{self.filename}'",
                    default_filename=oname,
                    wildcard=wildcard)

    def onClose(self, event=None):
        self.Destroy()

    def onImport(self, event=None):
        ignore = []
        for gname, chbox in self.overwrite_checkboxes.items():
            if not chbox.IsChecked():
                ignore.append(gname)

        sel_groups = self.grouplist.GetCheckedStrings()
        for fname, gname in self.allgroups.items():
            if fname not in sel_groups:
                ignore.append(gname)

        fname = self.filename.replace('\\','/')
        if fname.endswith('/'):
            fname = fname[:-1]
        lcmd = [f"load_session('{fname}'"]
        if len(ignore) > 0:
            ignore = repr(ignore)
            lcmd.append(f", ignore_groups={ignore}")
        if len(self.extra_groups) > 0:
            extra = repr(self.extra_groups)
            lcmd.append(f", include_xasgroups={extra}")

        lcmd = ''.join(lcmd) + ')'

        cmds = ["# Loading Larch Session with ", lcmd, '######']

        self.controller.larch.eval('\n'.join(cmds))
        last_fname = None
        xasgroups = getattr(self.controller.symtable, self.xasgroups_name, {})
        for key, val in xasgroups.items():
            if key not in self.controller.filelist.GetItems():
                self.controller.filelist.Append(key)
                last_fname = key

        self.controller.recentfiles.append((time.time(), self.filename))

        wx.CallAfter(self.Destroy)
        if last_fname is not None:
            self.parent.ShowFile(filename=last_fname)
