# Velog 글 관리

이 폴더는 게시 상태에 따라 세 부분으로 나눈다.

```text
velog-posts/
├─ published/  실제로 게시한 글
├─ drafts/     현재 작성·수정 중인 게시용 초안
└─ notes/      질문, 명령, 실험 과정이 담긴 원본 자료
```

## 현재 상태

| 상태 | 글 | 파일 |
| --- | --- | --- |
| 게시 완료 | 데스크탑을 팔고 미니 PC에 Proxmox를 올렸다 | `published/01-proxmox-setting-log.md` |
| 작성 중 | kubeadm으로 3노드 Kubernetes 클러스터 구성하기 | `drafts/02-kubeadm-kubernetes-cluster.md` |

게시한 글:

- [데스크탑을 팔고 미니 PC에 Proxmox를 올렸다](https://velog.io/@ch02/%EB%8D%B0%EC%8A%A4%ED%81%AC%ED%83%91%EC%9D%84-%ED%8C%94%EA%B3%A0-%EB%AF%B8%EB%8B%88-PC%EC%97%90-Proxmox%EB%A5%BC-%EC%98%AC%EB%A0%B8%EB%8B%A4)

## notes에 보관한 자료

| 파일 | 용도 |
| --- | --- |
| `proxmox-k8s-infra-preparation.md` | 첫 글의 다른 구성안 |
| `tailscale-subnet-router-deep-dive.md` | Tailscale subnet router 상세 설명 |
| `container-kubernetes-concepts.md` | containerd, CRI, cgroup, 네트워크 개념 질문 원본 |
| `kubeadm-kubernetes-cluster-study.md` | Kubernetes 클러스터 글의 장문 공부 원본 |
| `kubein-docker-cicd-prometheus-log.md` | Docker, GitHub Actions, Prometheus 작업 기록 |
| `kubein-sock-shop-cluster-troubleshooting.md` | Namespace, YAML, 디스크, MongoDB 장애 기록 |

## 앞으로의 작성 방식

1. 새로운 질문은 저장소 루트의 `QUESTION_LIST.md`에 기록한다.
2. 명령과 실험 결과는 관련 `notes` 파일에 남긴다.
3. 글로 만들 내용만 골라 `drafts`에 정리한다.
4. 실제 게시가 끝난 글은 `published`로 이동한다.
5. 원본 자료는 삭제하지 않고 Git 기록과 `notes`에 보관한다.

