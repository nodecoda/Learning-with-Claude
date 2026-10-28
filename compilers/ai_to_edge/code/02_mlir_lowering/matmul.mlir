// MLIR 14 语法：func 而不是 func.func（后者是 MLIR 15+ 的 Func dialect 全名）
module {
  func @matmul(%A: memref<4x8xf32>, %B: memref<8x4xf32>, %C: memref<4x4xf32>) {
    linalg.matmul ins(%A, %B : memref<4x8xf32>, memref<8x4xf32>)
                  outs(%C : memref<4x4xf32>)
    return
  }
}
