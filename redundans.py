#!/usr/bin/env python
desc="""Heterozygous genome assembly pipeline. It consists of three steps:
reduction, scaffolding and gap closing. 
"""
epilog="""Author:
l.p.pryszcz@gmail.com

Mizerow/Warsaw, 17/10/2014
"""

import os, resource, sys
import glob, subprocess, time
from datetime import datetime
import numpy as np

from fasta2homozygous import fasta2homozygous
from fastq2sspace import fastq2sspace
from fastq2insert_size import fastq2insert_size
from fasta_stats import fasta_stats

def timestamp():
    """Return formatted date-time string"""
    return "\n%s\n[%s] "%("#"*50, datetime.ctime(datetime.now()))

def symlink(file1, file2):
    """Create symbolic link taking care of real path."""
    if not os.path.isfile(file2):
        # check if need for absolute path
        file1abs = os.path.join(os.path.realpath(os.path.curdir), file1)
        if os.path.isfile(file1abs):
            os.symlink(file1abs, file2)
        # otherwise create symbolic link without full path
        else:
            os.symlink(file1, file2)

def get_orientation(pairs, fq1, fq2):
    """Return orientation of paired reads, either FF, FR, RF or RR.
    Warn if major orientation is represented by less than 90% of reads.
    """
    orientations = ('FF', 'FR', 'RF', 'RR')
    maxc = max(pairs)
    # get major orientation
    maxci = pairs.index(maxc)
    orientation = orientations[maxci]
    # get frac of total reads
    maxcfrac = 100.0 * maxc / sum(pairs)
    if maxcfrac < 90:
        info = "[WARNING] Poor quality: Major orientation (%s) represent %s%s of pairs in %s - %s: %s\n"
        sys.stderr.write(info%(orientation, maxcfrac, '%', fq1, fq2, str(pairs)))
    return orientation 

def get_libraries(fastq, fasta, mapq, threads, limit, verbose):
    """Return libraries"""
    # otherwise process all reads
    if not limit or limit<10e5:
        limit = 10e5
    
    # get libraries statistics using 1% of mapped read limit
    libdata = fastq2insert_size(sys.stderr, fastq, fasta, mapq, threads, \
                                limit/100, verbose)
    # separate paired-end & mate pairs
    ## also separate 300 and 600 paired-ends
    libraries = []
    # add libraries strating from lowest insert size
    for fq1, fq2, ismedian, ismean, isstd, pairs in sorted(libdata, key=lambda x: x[3]):
        # add new library set if 
        if not libraries or ismedian > 1.5*libraries[-1][4][0]:
            # libnames, libFs, libRs, orientations, libIS, libISStDev
            libraries.append([[], [], [], [], [], []])
            i = 1
        # add libname & fastq files
        libraries[-1][0].append("lib%s"%i)
        libraries[-1][1].append(open(fq1))
        libraries[-1][2].append(open(fq2))
        # orientation
        orientation = get_orientation(pairs, fq1, fq2)
        libraries[-1][3].append(orientation)
        # insert size information
        libraries[-1][4].append(int(ismean))
        stdfrac = isstd / ismean
        # capture large stdev
        if stdfrac > 0.66:
            sys.stderr.write("[WARNING] Highly variable insert size (%.f +- %.2f) in %s - %s!\n"%(ismean, isstd, fq1, fq2))
        # SSSPACE accepts stdfrac 0-1.0
        if stdfrac > 1:
            stdfrac = 1.0
        libraries[-1][5].append(stdfrac)
        # update counter
        i += 1
    return libraries

def get_read_limit(fastq, fasta, mapq, threads, readLimit, verbose):
    """Return read limit and libraries."""
    # limit no. of reads to align as fraction of genome size
    limit = 0
    if readLimit:
        stats = fasta_stats(open(fasta))
        fastaSize = int(stats.split('\t')[2])
        limit = int(readLimit * fastaSize)
        if verbose:
            sys.stderr.write(" Aligning %s mates per library...\n"%limit)

    # get libraries
    libraries = get_libraries(fastq, fasta, mapq, threads, limit, verbose)
    return limit, libraries
    
def run_scaffolding(outdir, scaffoldsFname, fastq, libraries, reducedFname, mapq, threads, \
                    joins, limit, iters, sspacebin, verbose, lib=""):
    """Execute scaffolding step."""        
    # run scaffolding using libraries with increasing insert size in multiple iterations
    pout = reducedFname
    i = 0
    #for i, (libnames, libFs, libRs, orients, libIS, libISStDev) in enumerate(libraries, 1):
    while i < len(libraries):
        libnames, libFs, libRs, orients, libIS, libISStDev = libraries[i]
        i += 1
        for j in range(1, iters+1):
            if verbose:
                sys.stderr.write(" iteration %s.%s ...\n"%(i,j))
            out = os.path.join(outdir, "_sspace.%s.%s"%(i, j))
            lib = ""
            # run fastq scaffolding
            fastq2sspace(out, open(pout), lib, libnames, libFs, libRs, orients, \
                         libIS, libISStDev, threads, mapq, limit, joins, \
                         sspacebin, verbose=0)
            # store out info
            pout = out+".fa"
            # link output ie out/_sspace.1.1/_sspace.1.1.scaffolds.fasta --> out/_sspace.1.1.scaffolds.fasta
            targetout = os.path.join(os.path.basename(out), os.path.basename(out+".final.scaffolds.fasta"))
            symlink(targetout, pout)
        # update library insert size estimation, especially for mate-pairs
        libraries = get_libraries(fastq, pout, mapq, threads, limit, verbose=0)
    # create symlink to final scaffolds or pout
    symlink(pout, scaffoldsFname)

    return libraries

def filter_reads(i, outdir, fq1, fq2, minlen, maxlen, limit, minqual):
    """Filter FastQ files and return output fnames."""
    fastq = (fq1, fq2)
    # generate output files
    fn1 = os.path.join(outdir, "_gapcloser.%s.%s.fq"%(i, os.path.basename(fq1.name)))
    fn2 = os.path.join(outdir, "_gapcloser.%s.%s.fq"%(i, os.path.basename(fq2.name)))
    out1 = open(fn1, "w")
    out2 = open(fn2, "w")
    outfiles = (out1, out2, 0, 0)
    # run filtering
    filter_paired(fastq, outfiles, minlen, maxlen, limit, minqual)
    out1.close()
    out2.close()
    return fn1, fn2
    
def prepare_gapcloser(outdir, mapq, configFn, libFs, libRs, orientations, libIS, libISStDev, \
                      minlen, maxlen, limit, verbose): 
    """Return SOAPdenovo2 config file needed by GapCloser."""
    lines  = "[LIB]\navg_ins=%s\nreverse_seq=%s\nasm_flags=3\nrank=%s\npair_num_cutoff=5\nmap_len=35\nq1=%s\nq2=%s\n"
    config = ["max_rd_len=%s"%maxlen]
    for i, (fq1, fq2, orient, iSize, iFrac) in enumerate(zip(libFs, libRs, orientations, \
                                                             libIS, libISStDev), 1):
        # consider skipping mate-pairs is libIS>1kb
        # skip orientations other than FR RF
        if orient == "FR":
            reverse_seq = 0
        elif orient == "RF":
            reverse_seq = 1
        else:
            if verbose:
                info = "  Skipping unsupported library %s in: %s - %s!\n"
                sys.stderr.write(info%(orient, fq1, fq2))
            continue
        # filter reads
        fn1, fn2 = filter_reads(i, outdir, fq1, fq2, minlen, maxlen, limit, mapq)
        #store config info
        config.append(lines%(iSize, reverse_seq, i, fn1, fn2))
        
    # store config only if some libs passed filtering
    if len(config)>1:
        with open(configFn, "w") as out:
            out.write("\n".join(config))
        return True
    
def run_gapclosing(outdir, mapq, libraries, nogapsFname, scaffoldsFname, \
                   threads, limit, iters, verbose, \
                   overlap=25, maxReadLen=150, minReadLen=40):
    """Execute gapclosing step."""
    pout = scaffoldsFname
    
    for i, (libnames, libFs, libRs, orientations, libIS, libISStDev) in enumerate(libraries, 1):
        # prepare config file and filter reads
        configFn = os.path.join(outdir, "_gapcloser.%s.conf"%i)
        # skip if not suitable libraries
        if not prepare_gapcloser(outdir, mapq, configFn, libFs, libRs, orientations, libIS, libISStDev, \
                                 minReadLen, maxReadLen, limit, verbose):
            continue
        # run iterations
        for j in range(1, iters+1):
            out = os.path.join(outdir, "_gapcloser.%s.%s.fa"%(i,j))
            # run GapCloser
            cmd = ["GapCloser", "-t %s"%threads, "-p %s"%overlap, "-l %s"%maxReadLen, \
                   "-a", pout, "-b", configFn, "-o", out]
            if verbose:
                sys.stderr.write(" iteration %s.%s ...\n"%(i,j))
                #sys.stderr.write( "  %s\n" % " ".join(cmd) )
            with open(out+".log", "w") as log:
                GapCloser = subprocess.Popen(cmd, bufsize=-1, stdout=log, stderr=log)
                GapCloser.wait()
            # store out info
            pout = out
    # create symlink to final scaffolds or pout
    symlink(pout, nogapsFname)
    
def run_gapclosing2(outdir, mapq, libraries, nogapsFname, scaffoldsFname, \
                    threads, limit, iters, verbose):
    """Execute gap closing using Gap2Seq."""
    pout = scaffoldsFname
    # iterate sets of libraries
    for i, (libnames, libFs, libRs, orientations, libIS, libISStDev) in enumerate(libraries, 1):
        reads = ""
        # iterate libraries withing the set
        for j, (fq1, fq2, orient, iSize, iFrac) in enumerate(zip(libFs, libRs, orientations, \
                                                                 libIS, libISStDev), 1):
            # consider skipping mate-pairs is libIS>1kb
            # skip orientations other than FR RF
            if orient == "FR":
                reverse_seq = 0
            elif orient == "RF":
                reverse_seq = 1
            else:
                if verbose:
                    info = "  Skipping unsupported library %s in: %s - %s!\n"
                    sys.stderr.write(info%(orient, fq1, fq2))
                continue
            # add reads
            reads += "%s,%s" % (fq1.name, fq2.name)
        # skip if not reads
        if not reads:
            continue
        out = os.path.join(outdir, "_gap2seq.%s.%s.fa"%(i,j))
        # run Gap2Seq
        cmd = ["Gap2Seq", "-nb-cores %s"%threads, "-scaffolds", pout, \
               "-filled", out, "-reads", reads]
        if verbose:
            sys.stderr.write(" iteration %s ...\n"%i)
            #sys.stderr.write( "  %s\n" % " ".join(cmd) )
        with open(out+".log", "w") as log:
            Gap2Seq = subprocess.Popen(cmd, bufsize=-1, stdout=log, stderr=log)
            Gap2Seq.wait()
        # store out info
        pout = out

    # create symlink to final scaffolds or pout
    symlink(pout, nogapsFname)
    
def redundants(fastq, fasta, outdir, mapq, threads, identity, overlap, minLength, \
               joins, readLimit, iters, sspacebin, reduction=1, scaffolding=1, gapclosing=1, \
               verbose=1, log=sys.stderr):
    """Launch redundans pipeline."""
    # redirect stderr
    #sys.stderr = log
    
    # prepare outdir or quit if exists
    if os.path.isdir(outdir):
        sys.stderr.write("Directory %s exists!\n"%outdir)
        sys.exit(1)
    else:
        os.makedirs(outdir)
    
    # REDUCTION
    contigsFname = os.path.join(outdir, "contigs.fa")
    reducedFname = os.path.join(outdir, "contigs.reduced.fa")
    # link contigs & genome
    symlink(fasta, contigsFname)    
    # get read limit & libraries
    limit, libraries = get_read_limit(fastq, contigsFname, mapq, threads, \
                                      readLimit, verbose)
    if reduction:
        if verbose:
            sys.stderr.write("%sReduction...\n"%timestamp())
            sys.stderr.write("#file name\tgenome size\tcontigs\theterozygous size\t[%]\theterozygous contigs\t[%]\tidentity [%]\tpossible joins\thomozygous size\t[%]\thomozygous contigs\t[%]\n")
        with open(reducedFname, "w") as out:
            fasta2homozygous(out, open(contigsFname), identity, overlap, \
                             minLength, libraries, limit)
    else:
        symlink(contigsFname, reducedFname)

    # SCAFFOLDING
    scaffoldsFname = os.path.join(outdir, "scaffolds.fa")
    if scaffolding:
        if verbose:
            sys.stderr.write("%sScaffolding...\n"%timestamp())
        # estimate read limit
        libraries = run_scaffolding(outdir, scaffoldsFname, fastq, libraries, reducedFname, mapq, \
                                    threads, joins, limit, iters, sspacebin, verbose)
    else:
        symlink(reducedFname, scaffoldsFname)
        
    # GAP CLOSING
    ## gapclosing is only necessary after scaffolding 
    nogapsFname = os.path.join(outdir, "scaffolds.filled.fa")
    if gapclosing and libraries:
        if verbose:
            sys.stderr.write("%sGap closing...\n"%timestamp())
        run_gapclosing(outdir, mapq, libraries, nogapsFname, scaffoldsFname, threads, \
                       limit, iters, verbose)
    else:
        symlink(scaffoldsFname, nogapsFname)

    # FASTA STATS
    if verbose:
        sys.stderr.write("%sReporting statistics...\n"%timestamp())
    # itermediate fasta files
    fastas  = [contigsFname, reducedFname]
    fastas += sorted(glob.glob(os.path.join(outdir, "_sspace.*.fa")))
    fastas.append(scaffoldsFname)
    fastas += sorted(glob.glob(os.path.join(outdir, "_gap*.fa")))
    fastas.append(nogapsFname)
    # report stats
    sys.stderr.write('#fname\tcontigs\tbases\tGC [%]\tcontigs >1kb\tbases in contigs >1kb\tN50\tN90\tNs\tlongest\n')
    for fn in fastas:
        sys.stderr.write(fasta_stats(open(fn)))

    # Clean-up
    # rm fq.is.txt
    # rm *.h5 CHANGEME.mphf

def _check_executable(cmd):
    """Check if executable exists."""
    p = subprocess.Popen("type " + cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return "".join(p.stdout.readlines())
        
def main():
    import argparse
    usage   = "%(prog)s -v" #usage=usage, 
    parser  = argparse.ArgumentParser(description=desc, epilog=epilog, \
                                      formatter_class=argparse.RawTextHelpFormatter)
  
    parser.add_argument("-v", dest="verbose",  default=False, action="store_true", help="verbose")    
    parser.add_argument('--version', action='version', version='0.11a')   
    parser.add_argument("-i", "--fastq", nargs="+", required=1, 
                        help="FASTQ PE/MP files")
    parser.add_argument("-f", "--fasta", required=1, 
                        help="assembly FASTA file")
    parser.add_argument("-o", "--outdir",  default="redundans", 
                        help="output directory [%(default)s]")
    parser.add_argument("-t", "--threads", default=2, type=int, 
                        help="max threads to run [%(default)s]")
    parser.add_argument("--log",           default=None, type=argparse.FileType('w'), 
                        help="output log to [stderr]")
    redu = parser.add_argument_group('Reduction options')
    redu.add_argument("--identity",        default=0.8, type=float,
                      help="min. identity [%(default)s]")
    redu.add_argument("--overlap",         default=0.75, type=float,
                      help="min. overlap  [%(default)s]")
    redu.add_argument("--minLength",       default=200, type=int, 
                      help="min. contig length [%(default)s]")
    ##missing redu options
    scaf = parser.add_argument_group('Scaffolding options')
    scaf.add_argument("-j", "--joins",  default=5, type=int, 
                      help="min pairs to join contigs [%(default)s]")
    scaf.add_argument("-l", "--limit",  default=0.2, type=float, 
                      help="align subset of reads [%(default)s]")
    scaf.add_argument("-q", "--mapq",    default=10, type=int, 
                      help="min mapping quality [%(default)s]")
    scaf.add_argument("--iters",         default=2, type=int, 
                      help="scaffolding iterations per library [%(default)s]")
    scaf.add_argument("--sspacebin",    default="~/src/SSPACE/SSPACE_Standard_v3.0.pl", 
                       help="SSPACE path  [%(default)s]")
    gaps = parser.add_argument_group('Gap closing options')
    skip = parser.add_argument_group('Skip below steps (all performed by default)')
    skip.add_argument('--noreduction',   action='store_false', default=True)   
    skip.add_argument('--noscaffolding', action='store_false', default=True)   
    skip.add_argument('--nogapclosing',  action='store_false', default=True)   
    
    o = parser.parse_args()
    if o.verbose:
        sys.stderr.write("Options: %s\n"%str(o))

    # check if input files exists
    for fn in [o.fasta,] + o.fastq: 
        if not os.path.isfile(fn):
            sys.stderr.write("No such file: %s\n"%fn)
            sys.exit(1)

    # check if all executables exists
    for cmd in ('blat', 'bwa', o.sspacebin, 'Gap2Seq'):
        info = _check_executable(cmd)
        if "not found" in info:
            sys.stderr.write("[ERROR] %s"%info)
            sys.exit(1)

    # initialise pipeline
    redundants(o.fastq, o.fasta, o.outdir, o.mapq, o.threads, \
               o.identity, o.overlap, o.minLength, \
               o.joins, o.limit, o.iters, o.sspacebin, \
               o.noreduction, o.noscaffolding, o.nogapclosing, \
               o.verbose, o.log)

if __name__=='__main__': 
    t0 = datetime.now()
    try:
        main()
    except KeyboardInterrupt:
        sys.stderr.write("\nCtrl-C pressed!      \n")
    except IOError as e:
        sys.stderr.write("I/O error({0}): {1}\n".format(e.errno, e.strerror))
    #[Errno 95] Operation not supported ie symlinks over samba or in NFS shares
    except OSError as e:
        sys.stderr.write("OS error({0}): {1}\n".format(e.errno, e.strerror))
    dt = datetime.now()-t0
    sys.stderr.write("#Time elapsed: %s\n"%dt)
