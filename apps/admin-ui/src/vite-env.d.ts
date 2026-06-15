/// <reference types="vite/client" />

// Self-hosted @fontsource-variable/* packages ship CSS only (no type declarations);
// declare them so their side-effect imports in main.tsx type-check.
declare module "@fontsource-variable/*";
