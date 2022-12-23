import os
from glob import glob
import numpy as np
import nibabel as nib
import nitransforms as nt
from joblib import Parallel, delayed

from .surface import Hemisphere
from .volume import mni_affine, mni_coords, canonical_volume_coords, aseg_mapping, extract_data_in_mni
from .resample import parse_combined_hdf5, compute_warp, parse_warp_image, interpolate


class Subject(object):
    def __init__(self, fs_dir=None, wf_root=None, mni_hdf5=None, do_surf=True, do_canonical=True, do_mni=True):
        self.fs_dir = fs_dir
        self.wf_root = wf_root
        self.mni_hdf5 = mni_hdf5
        self.do_surf = do_surf
        self.do_canonical = do_canonical
        self.do_mni = do_mni

        if self.do_surf or self.do_canonical:
            self.prepare_lta()

        if self.do_surf:
            self.prepare_surf()

        if self.do_canonical:
            self.prepare_canonical()

        if self.do_mni:
            self.prepare_mni()

    def prepare_lta(self):
        assert self.wf_root is not None
        lta_fn = os.path.join(
            self.wf_root, 'anat_preproc_wf', 'surface_recon_wf',
            't1w2fsnative_xfm', 'out.lta')
        self.lta = nt.io.lta.FSLinearTransform.from_filename(lta_fn).to_ras()

    def prepare_surf(self):
        assert self.fs_dir is not None
        self.hemispheres = {}
        for lr in 'lr':
            hemi = Hemisphere(lr, self.fs_dir)
            self.hemispheres[lr] = hemi

    def prepare_canonical(self):
        self.brainmask = nib.load(
            os.path.join(self.fs_dir, 'mri', 'brainmask.mgz'))
        canonical = canonical_volume_coords(self.brainmask)
        canonical = canonical @ self.lta.T
        self.canonical_coords = canonical

    def prepare_mni(self):
        assert self.mni_hdf5 is not None
        xyz1 = mni_coords.copy()
        affine, warp, warp_affine = parse_combined_hdf5(self.mni_hdf5)
        np.testing.assert_array_equal(warp_affine, mni_affine)
        diff = compute_warp(xyz1, warp, warp_affine, kwargs={'order': 1})
        xyz1[..., :3] += diff
        xyz1 = xyz1 @ affine.T
        self.mni_coords = xyz1

    def get_surface_data(self, lr, sphere_fn, space, proj='pial', resample='area'):
        hemi = self.hemispheres[lr]
        coords = hemi.get_coordinates(proj) @ self.lta.T
        xform = hemi.get_transformation(sphere_fn, space, resample)
        callback = lambda x: x.mean(axis=1) @ xform
        return coords, callback

    def get_volume_coords(self, use_mni=True):
        if use_mni:
            return self.mni_coords
        else:
            return self.canonical_coords


class FunctionalRun(object):
    # Temporarily removed the prefiltered data from Interpolator
    def __init__(self, wf_dir):
        self.wf_dir = wf_dir

        self.hmc = nt.io.itk.ITKLinearTransformArray.from_filename(
            f'{self.wf_dir}/bold_hmc_wf/fsl2itk/mat2itk.txt').to_ras()
        self.ref_to_t1 = nt.io.itk.ITKLinearTransform.from_filename(
            f'{self.wf_dir}/bold_reg_wf/bbreg_wf/concat_xfm/out_fwd.tfm').to_ras()

        nii_fns = sorted(glob(f'{self.wf_dir}/bold_split/vol*.nii.gz'))
        warp_fns = sorted(glob(f'{self.wf_dir}/unwarp_wf/resample/vol*_xfm.nii.gz'))
        if len(warp_fns):
            assert len(nii_fns) == len(warp_fns)

        self.nii_data, self.nii_affines = [], []
        for i, nii_fn in enumerate(nii_fns):
            nii = nib.load(nii_fn)
            data = np.asarray(nii.dataobj)
            self.nii_affines.append(nii.affine)
            self.nii_data.append(data)

        if len(warp_fns):
            self.warp_data, self.warp_affines = [], []
            for i, warp_fn in enumerate(warp_fns):
                warp_data, warp_affine = parse_warp_image(warp_fn)
                self.warp_data.append(warp_data)
                self.warp_affines.append(warp_affine)

        nii = nib.load(f'{self.wf_dir}/bold_t1_trans_wf/merge/vol0000_xform-00000_clipped_merged.nii')
        self.nii_t1 = np.asarray(nii.dataobj)
        self.nii_t1 = [self.nii_t1[..., _] for _ in range(self.nii_t1.shape[-1])]
        self.nii_t1_affine = nii.affine

    def interpolate(
            self, coords, onestep=True, interp_kwargs={'order': 1}, fill=np.nan, callback=None):
        if onestep:
            interps = interpolate_original_space(
                self.nii_data, self.nii_affines, coords,
                self.ref_to_t1, self.hmc, self.warp_data, self.warp_affines,
                interp_kwargs, fill, callback)
            return interps
        else:
            interps = interpolate_t1_space(
                self.nii_t1, self.nii_t1_affine, coords,
                interp_kwargs, fill, callback)
            return interps


def _combine_interpolation_results(interps):
    if isinstance(interps[0], np.ndarray):
        interps = np.stack(interps, axis=0)
        return interps
    if isinstance(interps[0], dict):
        keys = list(interps[0])
        output = {key: [] for key in keys}
        for interp in interps:
            for key in keys:
                output[key].append(interp[key])
        for key in keys:
            output[key] = np.stack(output[key], axis=0)
        return output
    raise ValueError


def interpolate_original_space(nii_data, nii_affines, coords,
        ref_to_t1, hmc, warp_data=None, warp_affines=None,
        interp_kwargs={'order': 1}, fill=np.nan, callback=None):
    coords = coords @ ref_to_t1.T
    interps = []
    for i, (data, affine) in enumerate(zip(nii_data, nii_affines)):
        cc = coords.copy()
        if warp_data is not None:
            diff = compute_warp(cc, warp_data[i].astype(np.float64), warp_affines[i])
            cc[..., :3] += diff
        cc = cc @ (hmc[i].T @ np.linalg.inv(affine).T)

        interp = interpolate(data.astype(np.float64), cc, fill=fill, kwargs=interp_kwargs)
        if callback is not None:
            interp = callback(interp)
        interps.append(interp)
    interps = _combine_interpolation_results(interps)

    return interps


def interpolate_t1_space(nii_t1, nii_t1_affine, coords,
        interp_kwargs={'order': 1}, fill=np.nan, callback=None):
    cc = coords @ np.linalg.inv(nii_t1_affine.T)
    interps = []
    for data in nii_t1:
        interp = interpolate(data.astype(np.float64), cc, fill=fill, kwargs=interp_kwargs)
        if callback is not None:
            interp = callback(interp)
            # interp = np.nanmean(interp, axis=1)
        interps.append(interp)
    interps = _combine_interpolation_results(interps)

    return interps


def workflow_single_run(label, sid, wf_root, out_dir, combinations, subj,
        tmpl_dir=os.path.expanduser('~/surface_template/lab/final')):
    label2 = label.replace('-', '_')
    wf_dir = (f'{wf_root}/func_preproc_{label2}_wf')
    assert os.path.exists(wf_dir)
    func_run = FunctionalRun(wf_dir)

    for lr in 'lr':
        for space, onestep, proj, resample in combinations:
            tag = '_'.join([('1step' if onestep else '2step'), proj, resample])
            out_fn = f'{out_dir}/{space}/{lr}-cerebrum/{tag}/sub-{sid}_{label}.npy'
            # out_fn = f'{out_dir}/{space}/{tag}/sub-{sid}_{label}_{lr}h.npy'
            if os.path.exists(out_fn):
                continue
            print(out_fn)
            os.makedirs(os.path.dirname(out_fn), exist_ok=True)

            a, b = space.split('-')
            if a == 'fsavg':
                name = 'fsaverage_' + b
            elif a == 'onavg':
                name = 'on-avg-1031-final_' + b
            else:
                name = space
            sphere_fn = f'{tmpl_dir}/{name}_{lr}h_sphere.npz'

            coords, callback = subj.get_surface_data(lr, sphere_fn, space, proj=proj, resample=resample)
            resampled = func_run.interpolate(
                coords, onestep, interp_kwargs={'order': 1}, fill=np.nan, callback=callback)

            np.save(out_fn, resampled)

    for mm in [2, 4]:
        space = f'mni-{mm}mm'
        tag = '1step_linear_overlap'
        rois = list(aseg_mapping.values())
        out_fns = [f'{out_dir}/{space}/{roi}/{tag}/sub-{sid}_{label}.npy' for roi in rois]
        if all([os.path.exists(_) for _ in out_fns]):
            continue
        for out_fn in out_fns:
            os.makedirs(os.path.dirname(out_fn), exist_ok=True)

        coords = subj.get_volume_coords(use_mni=True)
        callback = lambda x: extract_data_in_mni(x, mm=mm, cortex=True)
        output = func_run.interpolate(
            coords, True, interp_kwargs={'order': 1}, fill=np.nan, callback=callback)
        for roi, resampled in output.items():
            out_fn = f'{out_dir}/{space}/{roi}/{tag}/sub-{sid}_{label}.npy'
            os.makedirs(os.path.dirname(out_fn), exist_ok=True)
            np.save(out_fn, resampled)


    for mm in [2, 4]:
        space = f'mni-{mm}mm'
        tag = '1step_fmriprep_overlap'
        rois = list(aseg_mapping.values())
        out_fns = [f'{out_dir}/{space}/{roi}/{tag}/sub-{sid}_{label}.npy' for roi in rois]
        if all([os.path.exists(_) for _ in out_fns]):
            continue
        for out_fn in out_fns:
            os.makedirs(os.path.dirname(out_fn), exist_ok=True)

        in_fns = sorted(glob(os.path.join(
            wf_dir, 'bold_std_trans_wf', '_std_target_MNI152NLin2009cAsym.res1',
            'bold_to_std_transform', 'vol*_xform-*.nii.gz')))
        output = []
        for in_fn in in_fns:
            d = np.asanyarray(nib.load(in_fn).dataobj)
            output.append(extract_data_in_mni(d, mm=mm, cortex=True))
        output = _combine_interpolation_results(output)

        for roi, resampled in output.items():
            out_fn = f'{out_dir}/{space}/{roi}/{tag}/sub-{sid}_{label}.npy'
            np.save(out_fn, resampled)


def resample_workflow(
        sid, bids_dir, fs_dir, wf_root, out_dir,
        combinations=[
            ('onavg-ico32', '1step_pial_area'),
        ],
        n_jobs=1,
    ):

    raw_bolds = sorted(glob(f'{bids_dir}/sub-{sid}/ses-*/func/*_bold.nii.gz')) + \
        sorted(glob(f'{bids_dir}/sub-{sid}/func/*_bold.nii.gz'))
    labels = [os.path.basename(_).split(f'sub-{sid}_', 1)[1].rsplit('_bold.nii.gz', 1)[0] for _ in raw_bolds]

    new_combinations = []
    for a, b in combinations[::-1]:
        b, c = b.split('_', 1)
        c, d = c.rsplit('_', 1)
        b = {'1step': True, '2step': False}[b]
        new_combinations.append([a, b, c, d])
    combinations = new_combinations

    mni_hdf5 = os.path.join(wf_root, 'anat_preproc_wf', 'anat_norm_wf', '_template_MNI152NLin2009cAsym',
                            'registration', 'ants_t1_to_mniComposite.h5')

    subj = Subject(fs_dir=fs_dir, wf_root=wf_root, mni_hdf5=mni_hdf5, do_surf=True, do_canonical=True, do_mni=True)

    jobs = [
        delayed(workflow_single_run)(label, sid, wf_root, out_dir, combinations, subj)
        for label in labels]
    with Parallel(n_jobs=n_jobs) as parallel:
        parallel(jobs)

    # brainmask = nib.load(f'{fs_dir}/mri/brainmask.mgz')
    # canonical = nib.as_closest_canonical(brainmask)
    # boundaries = find_truncation_boundaries(np.asarray(canonical.dataobj))
    # for key in ['T1', 'T2', 'brainmask', 'ribbon']:
    #     out_fn = f'{out_dir}/average-volume/sub-{sid}_{key}.npy'
    #     img = nib.load(f'{fs_dir}/mri/{key}.mgz')
    #     canonical = nib.as_closest_canonical(img)
    #     data = np.asarray(canonical.dataobj)
    #     data = data[boundaries[0, 0]:boundaries[0, 1], boundaries[1, 0]:boundaries[1, 1], boundaries[2, 0]:boundaries[2, 1]]
    #     np.save(out_fn, data)

#     out_fn = f'{out_dir}/average-volume/sub-{sid}_{label}.npy'
#     if not os.path.exists(out_fn):
#         if interpolator is None:
#             interpolator = Interpolator(sid, label, fs_dir, wf_dir)
#             interpolator.prepare(orders=[1])
#         vol = np.mean(interpolator.interpolate_volume(), axis=0)
#         os.makedirs(os.path.dirname(out_fn), exist_ok=True)
#         np.save(out_fn, vol)
