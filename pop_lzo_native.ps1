param(
    [Parameter(Mandatory = $true)] [string] $InputFile,
    [Parameter(Mandatory = $true)] [string] $OutputFile
)

$ErrorActionPreference = 'Stop'

Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;

public static class MiniJadeLzoNative
{
    [DllImport("lzo.dll")]
    public static extern int __lzo_init3();

    [DllImport("lzo.dll")]
    public static extern int lzo1x_1_compress(
        byte[] src,
        int src_len,
        byte[] dst,
        ref int dst_len,
        byte[] wrkmem);
}
'@

$kernel = Add-Type -MemberDefinition '[DllImport("kernel32.dll")] public static extern bool SetDllDirectory(string path);' -Name MiniJadeKernel -Namespace MiniJade -PassThru
[MiniJade.MiniJadeKernel]::SetDllDirectory($PSScriptRoot) | Out-Null

$init = [MiniJadeLzoNative]::__lzo_init3()
if ($init -ne 0) { throw "LZO initialization failed (rc=$init)." }

$input = [IO.File]::ReadAllBytes($InputFile)
if ($input.Length -lt 8 -or [BitConverter]::ToString($input, 4, 4).Replace('-', '') -ne '99C0FFEE') {
    throw 'Input does not contain POP magic 99C0FFEE at offset 4.'
}

$blockSize = 131072
$result = New-Object IO.MemoryStream
$position = 0
while ($position -lt $input.Length) {
    $count = [Math]::Min($blockSize, $input.Length - $position)
    $block = New-Object byte[] $count
    [Array]::Copy($input, $position, $block, 0, $count)
    $dst = New-Object byte[] ($count + [Math]::Floor($count / 64) + 64 + 8)
    $outLength = $dst.Length
    $work = New-Object byte[] 65536
    $rc = [MiniJadeLzoNative]::lzo1x_1_compress($block, $count, $dst, [ref] $outLength, $work)
    if ($rc -ne 0) { throw "LZO compression failed (rc=$rc, block=$count bytes)." }

    $result.Write([BitConverter]::GetBytes([int]$count), 0, 4)
    if ($outLength -gt $count) {
        $result.Write([BitConverter]::GetBytes([int]$count), 0, 4)
        $result.Write($block, 0, $block.Length)
    } else {
        $result.Write([BitConverter]::GetBytes([int]$outLength), 0, 4)
        $result.Write($dst, 0, $outLength)
    }
    $position += $count
}

$payloadLength = [int]$result.Length
if ((($payloadLength + 4) % 2048) -ne 0) {
    $diff = (([Math]::Floor($payloadLength / 2048) * 2048) + 2044) - $payloadLength
    if ($diff -gt 0) { $result.Write((New-Object byte[] $diff), 0, $diff) }
}

[IO.File]::WriteAllBytes($OutputFile, $result.ToArray())
