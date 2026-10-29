#include <windows.h>
#include <d3d11.h>
#include <dxgi1_6.h>
#include <wrl/client.h>
#include <fstream>
#include <iostream>
#include <stdexcept>
#include <cstdint>
#include <fcntl.h>
#include <io.h>
#include <vector>
#include <winrt/Windows.Foundation.h>
#include <winrt/Windows.Graphics.Capture.h>
#include <winrt/Windows.Graphics.DirectX.Direct3D11.h>
#include <windows.graphics.capture.interop.h>
#include <windows.graphics.directx.direct3d11.interop.h>
using Microsoft::WRL::ComPtr;
static void check(HRESULT hr) { if (FAILED(hr)) { std::cerr << "HRESULT 0x" << std::hex << hr << "\n"; throw std::runtime_error("DirectX failure"); } }
static BOOL CALLBACK findGame(HWND w, LPARAM p) {
    wchar_t title[512]; GetWindowTextW(w, title, 512);
    if (IsWindowVisible(w) && wcsstr(title, L"PUBG:")) *reinterpret_cast<HWND*>(p)=w;
    return TRUE;
}
static double sdrWhite(const wchar_t* deviceName) {
    UINT32 count=0,modesCount=0;
    if(GetDisplayConfigBufferSizes(QDC_ONLY_ACTIVE_PATHS,&count,&modesCount)) return 0;
    std::vector<DISPLAYCONFIG_PATH_INFO> paths(count);
    std::vector<DISPLAYCONFIG_MODE_INFO> modes(modesCount);
    if(QueryDisplayConfig(QDC_ONLY_ACTIVE_PATHS,&count,paths.data(),&modesCount,modes.data(),nullptr)) return 0;
    for(UINT32 i=0;i<count;++i) {
        DISPLAYCONFIG_SOURCE_DEVICE_NAME source{};
        source.header={DISPLAYCONFIG_DEVICE_INFO_GET_SOURCE_NAME,sizeof(source),paths[i].sourceInfo.adapterId,paths[i].sourceInfo.id};
        if(DisplayConfigGetDeviceInfo(&source.header) || wcscmp(source.viewGdiDeviceName,deviceName)) continue;
        DISPLAYCONFIG_SDR_WHITE_LEVEL white{};
        white.header={DISPLAYCONFIG_DEVICE_INFO_GET_SDR_WHITE_LEVEL,sizeof(white),paths[i].targetInfo.adapterId,paths[i].targetInfo.id};
        if(!DisplayConfigGetDeviceInfo(&white.header)) return 80.0*white.SDRWhiteLevel/1000.0;
    }
    return 0;
}
int wmain(int argc, wchar_t** argv) { try {
    if(argc!=2 && argc!=3) throw std::runtime_error("Usage: capture-hdr OUTPUT.dds|- [HWND]");
    SetProcessDPIAware(); winrt::init_apartment(winrt::apartment_type::multi_threaded);
    HWND game=nullptr;
    if(argc==3) game=reinterpret_cast<HWND>(std::stoull(argv[2]));
    else { EnumWindows(findGame,reinterpret_cast<LPARAM>(&game)); std::cerr << "Waiting 10 seconds for PUBG foreground...\n"; Sleep(10000); }
    if(!game || !IsWindow(game) || IsIconic(game)) throw std::runtime_error("Game window absent/minimized");
    if(GetForegroundWindow()!=game) throw std::runtime_error("Game is not foreground; no capture saved");
    HMONITOR monitor=MonitorFromWindow(game,MONITOR_DEFAULTTONEAREST);
    ComPtr<IDXGIFactory1> factory; check(CreateDXGIFactory1(IID_PPV_ARGS(&factory)));
    ComPtr<IDXGIAdapter1> selectedAdapter; ComPtr<IDXGIOutput> selectedOutput;
    for(UINT ai=0;;++ai) {
        ComPtr<IDXGIAdapter1> adapter; auto hr=factory->EnumAdapters1(ai,&adapter);
        if(hr==DXGI_ERROR_NOT_FOUND) break; check(hr);
        for(UINT oi=0;;++oi) {
            ComPtr<IDXGIOutput> output; hr=adapter->EnumOutputs(oi,&output);
            if(hr==DXGI_ERROR_NOT_FOUND) break; check(hr);
            DXGI_OUTPUT_DESC desc; check(output->GetDesc(&desc));
            if(desc.Monitor==monitor) { selectedAdapter=adapter; selectedOutput=output; }
        }
    }
    if(!selectedOutput) throw std::runtime_error("Game monitor not found");
    ComPtr<IDXGIOutput6> output6; check(selectedOutput.As(&output6));
    DXGI_OUTPUT_DESC1 od; check(output6->GetDesc1(&od));
    double whiteNits=sdrWhite(od.DeviceName);
    ComPtr<ID3D11Device> device; ComPtr<ID3D11DeviceContext> context;
    check(D3D11CreateDevice(selectedAdapter.Get(),D3D_DRIVER_TYPE_UNKNOWN,nullptr,0,nullptr,0,D3D11_SDK_VERSION,&device,nullptr,&context));
    namespace wgc=winrt::Windows::Graphics::Capture;
    namespace wd3d=winrt::Windows::Graphics::DirectX::Direct3D11;
    ComPtr<IDXGIDevice> dxgiDevice; check(device.As(&dxgiDevice));
    winrt::com_ptr<IInspectable> inspectable;
    check(CreateDirect3D11DeviceFromDXGIDevice(dxgiDevice.Get(),inspectable.put()));
    auto captureDevice=inspectable.as<wd3d::IDirect3DDevice>();
    auto interop=winrt::get_activation_factory<wgc::GraphicsCaptureItem,IGraphicsCaptureItemInterop>();
    wgc::GraphicsCaptureItem item{nullptr};
    check(interop->CreateForMonitor(monitor,winrt::guid_of<wgc::GraphicsCaptureItem>(),winrt::put_abi(item)));
    auto pool=wgc::Direct3D11CaptureFramePool::CreateFreeThreaded(captureDevice,winrt::Windows::Graphics::DirectX::DirectXPixelFormat::R16G16B16A16Float,2,item.Size());
    auto session=pool.CreateCaptureSession(item);
    session.IsCursorCaptureEnabled(false);
    session.StartCapture();
    wgc::Direct3D11CaptureFrame captured{nullptr};
    for(int i=0;i<500 && !captured;++i) { captured=pool.TryGetNextFrame(); if(!captured) Sleep(10); }
    if(!captured) throw std::runtime_error("No WGC frame within 5 seconds");
    if(GetForegroundWindow()!=game) throw std::runtime_error("Focus changed during capture; frame discarded");
    auto access=captured.Surface().as<::Windows::Graphics::DirectX::Direct3D11::IDirect3DDxgiInterfaceAccess>();
    ComPtr<ID3D11Texture2D> frame; check(access->GetInterface(IID_PPV_ARGS(&frame)));
    D3D11_TEXTURE2D_DESC td; frame->GetDesc(&td);
    if(td.Format!=DXGI_FORMAT_R16G16B16A16_FLOAT) { throw std::runtime_error("Capture is not FP16; refusing SDR fallback"); }
    td.Usage=D3D11_USAGE_STAGING; td.BindFlags=0; td.CPUAccessFlags=D3D11_CPU_ACCESS_READ; td.MiscFlags=0;
    ComPtr<ID3D11Texture2D> staging; check(device->CreateTexture2D(&td,nullptr,&staging));
    context->CopyResource(staging.Get(),frame.Get());
    D3D11_MAPPED_SUBRESOURCE mapped; check(context->Map(staging.Get(),0,D3D11_MAP_READ,0,&mapped));
    // DDS DX10 header preserves the original little-endian RGBA half floats.
    uint32_t h[37]{}; h[0]=0x20534444; h[1]=124; h[2]=0x100F;
    h[3]=td.Height; h[4]=td.Width; h[5]=td.Width*8;
    h[19]=32; h[20]=4; h[21]=0x30315844; h[27]=0x1000;
    h[32]=10; h[33]=3; h[35]=1;
    std::ofstream file;
    bool pipe=wcscmp(argv[1],L"-")==0;
    if(pipe) _setmode(_fileno(stdout),_O_BINARY); else file.open(argv[1],std::ios::binary);
    std::ostream& out=pipe?std::cout:file;
    // Explicitly armed one-frame diagnostic; expires after ten minutes.
    WIN32_FILE_ATTRIBUTE_DATA armed{};
    const wchar_t* flag=L"build-work/temp/next-hdr.flag";
    if(pipe && GetFileAttributesExW(flag,GetFileExInfoStandard,&armed)) {
        FILETIME now; GetSystemTimeAsFileTime(&now);
        ULARGE_INTEGER a{},n{}; a.LowPart=armed.ftLastWriteTime.dwLowDateTime; a.HighPart=armed.ftLastWriteTime.dwHighDateTime;
        n.LowPart=now.dwLowDateTime; n.HighPart=now.dwHighDateTime;
        if(n.QuadPart>=a.QuadPart && n.QuadPart-a.QuadPart<=600ULL*10000000) {
            const wchar_t* diagnostic=L"inspection/genshin-hdr-next.dds";
            if(GetFileAttributesW(diagnostic)==INVALID_FILE_ATTRIBUTES) {
                std::ofstream raw(L"inspection/genshin-hdr-next.dds.tmp",std::ios::binary);
                raw.write(reinterpret_cast<char*>(h),sizeof(h));
                for(UINT y=0;y<td.Height;++y) raw.write(static_cast<char*>(mapped.pData)+y*mapped.RowPitch,td.Width*8);
                raw.flush(); bool ok=raw.good(); raw.close();
                if(ok && MoveFileW(L"inspection/genshin-hdr-next.dds.tmp",diagnostic)) DeleteFileW(flag);
            }
        } else DeleteFileW(flag);
    }
    out.write(reinterpret_cast<char*>(h),sizeof(h));
    for(UINT y=0;y<td.Height;++y) out.write(static_cast<char*>(mapped.pData)+y*mapped.RowPitch,td.Width*8);
    out.flush(); bool saved=out.good(); context->Unmap(staging.Get(),0); captured.Close(); session.Close(); pool.Close();
    if(!saved) throw std::runtime_error("DDS write failed");
    std::cerr << "{\"width\":"<<td.Width<<",\"height\":"<<td.Height<<",\"dxgi_format\":"<<td.Format
      <<",\"output_color_space\":"<<od.ColorSpace<<",\"output_bits_per_color\":"<<od.BitsPerColor
      <<",\"max_luminance_nits\":"<<od.MaxLuminance<<",\"sdr_white_nits\":"<<whiteNits<<"}\n";
    return 0;
} catch(const winrt::hresult_error& e) { std::cerr << "WGC HRESULT 0x" << std::hex << e.code().value << "\n"; return 1; } catch(const std::exception& e) { std::cerr<<e.what()<<"\n"; return 1; } }
