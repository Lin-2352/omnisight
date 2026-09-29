// Browser-side image intake: validate, downscale on an off-screen canvas, JPEG-encode, base64.
// Mirrors the desktop client's budget: at most 1280x720 and 350 KB of base64.

export const ACCEPTED_TYPES = ["image/jpeg", "image/png", "image/webp"] as const;
export type AcceptedType = (typeof ACCEPTED_TYPES)[number];

export const MAX_UPLOAD_BYTES = 10 * 1024 * 1024;
export const MAX_WIDTH = 1280;
export const MAX_HEIGHT = 720;
export const MAX_BASE64_BYTES = 350 * 1024;
const QUALITY_LADDER = [0.75, 0.65, 0.55, 0.45, 0.35] as const;

export class ImageInputError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ImageInputError";
  }
}

export interface EncodedImage {
  base64: string;
  mime: "image/jpeg";
  width: number;
  height: number;
  originalWidth: number;
  originalHeight: number;
  originalSize: number;
  compressedSize: number;
  quality: number;
  elapsedMs: number;
}

export function isAcceptedType(type: string): type is AcceptedType {
  return (ACCEPTED_TYPES as readonly string[]).includes(type);
}

/** Reject unsupported or oversized files before any decoding happens. */
export function validateImageFile(file: Blob): void {
  if (!isAcceptedType(file.type)) {
    throw new ImageInputError(`Unsupported file type "${file.type || "unknown"}". Use a JPEG, PNG or WebP screenshot.`);
  }
  if (file.size > MAX_UPLOAD_BYTES) {
    throw new ImageInputError(`File is ${(file.size / 1048576).toFixed(1)} MB; the limit is 10 MB.`);
  }
  if (file.size === 0) {
    throw new ImageInputError("The file is empty.");
  }
}

/** Base64-encode bytes in chunks (String.fromCharCode on a huge array overflows the stack). */
export function bytesToBase64(bytes: Uint8Array): string {
  let binary = "";
  const chunk = 0x8000;
  for (let offset = 0; offset < bytes.length; offset += chunk) {
    binary += String.fromCharCode(...bytes.subarray(offset, offset + chunk));
  }
  return btoa(binary);
}

export async function blobToBase64(blob: Blob): Promise<string> {
  return bytesToBase64(new Uint8Array(await blob.arrayBuffer()));
}

export function fitWithin(width: number, height: number): { width: number; height: number } {
  const scale = Math.min(1, MAX_WIDTH / width, MAX_HEIGHT / height);
  return { width: Math.max(1, Math.round(width * scale)), height: Math.max(1, Math.round(height * scale)) };
}

async function canvasToJpeg(canvas: OffscreenCanvas | HTMLCanvasElement, quality: number): Promise<Blob> {
  if ("convertToBlob" in canvas) {
    return canvas.convertToBlob({ type: "image/jpeg", quality });
  }
  return new Promise<Blob>((resolve, reject) => {
    canvas.toBlob(
      (blob) => (blob ? resolve(blob) : reject(new ImageInputError("The browser could not encode the image."))),
      "image/jpeg",
      quality,
    );
  });
}

/** Validate, fit within 1280x720 on an off-screen canvas, and encode as JPEG q0.75 (lower only if needed). */
export async function compressAndEncodeImage(file: Blob): Promise<EncodedImage> {
  validateImageFile(file);
  const started = performance.now();
  let bitmap: ImageBitmap;
  try {
    bitmap = await createImageBitmap(file);
  } catch {
    throw new ImageInputError("The file could not be decoded as an image.");
  }
  const originalWidth = bitmap.width;
  const originalHeight = bitmap.height;
  const target = fitWithin(originalWidth, originalHeight);
  const canvas: OffscreenCanvas | HTMLCanvasElement =
    typeof OffscreenCanvas !== "undefined"
      ? new OffscreenCanvas(target.width, target.height)
      : Object.assign(document.createElement("canvas"), target);
  const context = canvas.getContext("2d") as OffscreenCanvasRenderingContext2D | CanvasRenderingContext2D | null;
  if (!context) {
    bitmap.close();
    throw new ImageInputError("Canvas 2D is not available in this browser.");
  }
  context.imageSmoothingQuality = "high";
  context.drawImage(bitmap, 0, 0, target.width, target.height);
  bitmap.close();

  for (const quality of QUALITY_LADDER) {
    const blob = await canvasToJpeg(canvas, quality);
    const base64 = await blobToBase64(blob);
    if (base64.length <= MAX_BASE64_BYTES) {
      return {
        base64,
        mime: "image/jpeg",
        width: target.width,
        height: target.height,
        originalWidth,
        originalHeight,
        originalSize: file.size,
        compressedSize: base64.length,
        quality,
        elapsedMs: Math.round(performance.now() - started),
      };
    }
  }
  throw new ImageInputError("The screenshot is too detailed to fit the 350 KB budget even at low quality.");
}
