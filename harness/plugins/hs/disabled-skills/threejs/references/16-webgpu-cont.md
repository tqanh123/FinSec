# WebGPU Rendering (continued 2/2)

## Browser Support

Browser support changes quickly; check https://caniuse.com/webgpu before targeting a browser (Chrome/Edge 113+ shipped first).

Check support: `WebGPU.isAvailable()`

## Best Practices

- Use compute shaders for particle systems, physics
- Leverage storage buffers for large datasets
- Async compile before rendering
- Use Node materials instead of custom GLSL
- Test on both WebGL and WebGPU
- Provide WebGL fallback for unsupported browsers
